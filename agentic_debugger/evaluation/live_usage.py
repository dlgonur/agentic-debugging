"""Request/token usage accounting for live evaluation.

This module owns usage truth: the per-attempt canonical evaluation
(``_AttemptUsage``/``_compute_attempt_usage``), the session-cumulative
``LiveModelMetrics`` authority, and the per-logical-call provider-reported
aggregation (``_LogicalRequestUsage``) consumed by the controller after
each model request.  Transport attempts that fail without a
provider-completed response contribute nothing; counts are never
fabricated."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from agentic_debugger.agent.token_usage import TokenUsage, TokenUsageCoverage, usage_from_transport


@dataclass(frozen=True)
class _AttemptUsage:
    reported: bool
    input_bound: int | None
    input_complete: bool
    output_bound: int | None
    output_complete: bool
    cached_bound: int | None
    cached_complete: bool
    cache_write_bound: int | None
    cache_write_complete: bool
    exact_total: int | None
    total_bound: int
    total_complete: bool


def _compute_attempt_usage(raw_usage: TokenUsage | None) -> _AttemptUsage:
    """Evaluate one provider-completed transport attempt under canonical truth.

    - Clamps cached tokens if they exceed complete input.
    - If input tokens is reported, complete input is known (input_complete=True).
      Complete input already includes cache contribution under this contract.
    - If input tokens is None but cached_input_tokens (or cache_write) is reported,
      because cache is a subset/disjoint-bucket of input, this proves a truthful
      lower bound without estimation (input_complete=False).
    - If output tokens is reported, output_complete=True; else output_complete=False.
    - Determines attempt total and completeness:
      Rule 1: If input_complete and output_complete, exact total = input + output.
      Rule 2: Else if a valid provider total is reported (and >= component bounds),
              exact total = that reported total.
      Rule 3: Else exact total is unknown; total_bound is max(components, provider total).
    """
    if raw_usage is None or not raw_usage.reported:
        return _AttemptUsage(
            reported=False,
            input_bound=None,
            input_complete=False,
            output_bound=None,
            output_complete=False,
            cached_bound=None,
            cached_complete=False,
            cache_write_bound=None,
            cache_write_complete=False,
            exact_total=None,
            total_bound=0,
            total_complete=False,
        )

    in_tok = raw_usage.input_tokens
    out_tok = raw_usage.output_tokens
    cached_tok = raw_usage.cached_input_tokens
    write_tok = raw_usage.cache_write_input_tokens
    tot_tok = raw_usage.total_tokens

    # For a single attempt, cached cannot exceed input when input is complete:
    if cached_tok is not None and in_tok is not None and cached_tok > in_tok:
        cached_tok = None

    # Cached completeness and lower bound
    cached_bound = cached_tok
    cached_complete = cached_tok is not None

    # Cache write completeness and lower bound
    cache_write_bound = write_tok
    cache_write_complete = write_tok is not None

    # Input completeness and lower bound (F6):
    if in_tok is not None:
        input_bound = in_tok
        input_complete = True
    else:
        cache_contrib = (cached_tok or 0) + (write_tok or 0)
        if cached_tok is not None or write_tok is not None:
            input_bound = cache_contrib
            input_complete = False
        else:
            input_bound = None
            input_complete = False

    # Output completeness and lower bound:
    if out_tok is not None:
        output_bound = out_tok
        output_complete = True
    else:
        output_bound = None
        output_complete = False

    # Total and total completeness:
    min_components = (input_bound or 0) + (output_bound or 0)
    if input_complete and output_complete:
        attempt_total = input_bound + output_bound
        exact_total = attempt_total
        total_bound = attempt_total
        total_complete = True
    elif tot_tok is not None and tot_tok >= min_components:
        attempt_total = tot_tok
        exact_total = attempt_total
        total_bound = attempt_total
        total_complete = True
    else:
        exact_total = None
        total_complete = False
        total_bound = max(min_components, tot_tok or 0)

    return _AttemptUsage(
        reported=True,
        input_bound=input_bound,
        input_complete=input_complete,
        output_bound=output_bound,
        output_complete=output_complete,
        cached_bound=cached_bound,
        cached_complete=cached_complete,
        cache_write_bound=cache_write_bound,
        cache_write_complete=cache_write_complete,
        exact_total=exact_total,
        total_bound=total_bound,
        total_complete=total_complete,
    )


@dataclass
class LiveModelMetrics:
    model_requests:int=0; model_responses:int=0; logical_model_calls:int=0; transport_attempts:int=0; cumulative_request_bytes:int=0; max_request_bytes:int=0; stream_frame_count:int=0; thinking_bytes:int=0; action_content_bytes:int=0; retries:int=0; directive_repairs:int=0; provider_errors:int=0; provider_error_kinds:list[str]=field(default_factory=list); directive_rejections:int=0; directive_rejection_categories:list[str]=field(default_factory=list); prompt_tokens:int|None=None; completion_tokens:int|None=None; total_tokens:int|None=None; cached_input_tokens:int|None=None; cache_write_input_tokens:int|None=None; usage_reported:bool=False; usage_missing_fields:list[str]=field(default_factory=list); termination_reason:str|None=None; controller_wall_duration_ms:int=0; verifier_wall_duration_ms:int=0
    def error(self,kind):
        self.provider_errors+=1
        if kind not in self.provider_error_kinds: self.provider_error_kinds.append(kind)
    def directive_rejection(self, category):
        self.directive_rejections+=1
        if category not in self.directive_rejection_categories: self.directive_rejection_categories.append(category)
    def directive_repair(self):
        self.directive_repairs+=1
    def usage(self, value):
        # Session-cumulative provider-reported usage (the accepted evaluation
        # usage authority).  Normalization, per-dimension unknownness, and
        # the counts-only safety contract live in ``agent.token_usage``.
        fields = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
        )
        if not isinstance(value, Mapping):
            self.usage_missing_fields.extend(
                x for x in fields if x not in self.usage_missing_fields
            )
            return

        raw_usage = usage_from_transport(value)
        if not raw_usage.reported:
            self.usage_missing_fields.extend(
                x for x in fields if x not in self.usage_missing_fields
            )
            return

        attempt = _compute_attempt_usage(raw_usage)
        if not attempt.reported:
            self.usage_missing_fields.extend(
                x for x in fields if x not in self.usage_missing_fields
            )
            return

        self.usage_reported = True

        total_num = (
            attempt.exact_total
            if attempt.total_complete
            else (attempt.total_bound if attempt.total_bound > 0 else None)
        )

        attempt_fields = (
            ("prompt_tokens", attempt.input_bound, attempt.input_complete),
            ("completion_tokens", attempt.output_bound, attempt.output_complete),
            ("total_tokens", total_num, attempt.total_complete),
            ("cached_input_tokens", attempt.cached_bound, attempt.cached_complete),
            ("cache_write_input_tokens", attempt.cache_write_bound, attempt.cache_write_complete),
        )

        for name, number, is_complete in attempt_fields:
            if number is not None:
                old = getattr(self, name)
                setattr(self, name, number if old is None else old + number)
                if not is_complete and name not in self.usage_missing_fields:
                    self.usage_missing_fields.append(name)
            elif name not in self.usage_missing_fields:
                self.usage_missing_fields.append(name)
    def activity(self,value):
        if not isinstance(value,Mapping): return
        for source,target in (("stream_frame_count","stream_frame_count"),("thinking_bytes","thinking_bytes"),("content_bytes","action_content_bytes")):
            number=value.get(source)
            if type(number) is int and number>=0: setattr(self,target,getattr(self,target)+number)
    def to_mapping(self): return {"model_request_count":self.model_requests,"model_response_count":self.model_responses,"logical_model_call_count":self.logical_model_calls,"transport_attempt_count":self.transport_attempts,"cumulative_request_bytes":self.cumulative_request_bytes,"max_request_bytes":self.max_request_bytes,"stream_frame_count":self.stream_frame_count,"thinking_bytes":self.thinking_bytes,"action_content_bytes":self.action_content_bytes,"retry_count":self.retries,"directive_repair_count":self.directive_repairs,"provider_error_count":self.provider_errors,"provider_error_kinds":self.provider_error_kinds,"directive_rejection_count":self.directive_rejections,"directive_rejection_categories":self.directive_rejection_categories,"token_usage":{"prompt_tokens":self.prompt_tokens,"completion_tokens":self.completion_tokens,"total_tokens":self.total_tokens,"cached_input_tokens":self.cached_input_tokens,"cache_write_input_tokens":self.cache_write_input_tokens,"provider_reported":self.usage_reported,"missing_fields":sorted(set(self.usage_missing_fields))},"termination_reason":self.termination_reason,"controller_wall_duration_ms":self.controller_wall_duration_ms,"verifier_wall_duration_ms":self.verifier_wall_duration_ms}


class _LogicalRequestUsage:
    """Provider-reported usage aggregation for one logical model call.

    Every provider-completed transport attempt inside the call counts,
    including attempts whose directive was later rejected (directive
    repairs) and attempts followed by transport retries: each reported
    response consumed real tokens.  Transport attempts that failed
    without a provider-completed response contribute nothing and never
    receive fabricated counts.

    Each attempt is evaluated under its own canonical truth first:
    1. If valid Input and Output are both known, exact attempt Total =
       Input + Output (overriding conflicting raw provider Total).
    2. Else if a valid provider Total is reported (and >= known
       component lower bounds), exact attempt Total = that reported Total.
    3. Else exact Total is unknown, preserving any truthful lower bound
       from reported Input/Output.

    Across attempts, attempt totals and lower bounds accumulate:
    - Logical Total is COMPLETE only if every provider-completed attempt
      had a complete exact total.
    - If any completed attempt lacked exact total, logical Total retains
      the truthful lower bound and is marked partial.
    """

    __slots__ = (
        "_completed_attempts",
        "_reported_attempts",
        "_accum_input",
        "_accum_output",
        "_accum_cached",
        "_accum_cache_write",
        "_attempts_with_input",
        "_attempts_with_output",
        "_attempts_with_cached",
        "_attempts_with_cache_write",
        "_exact_attempt_totals",
        "_attempt_total_bounds",
    )

    def __init__(self) -> None:
        self._completed_attempts = 0
        self._reported_attempts = 0
        self._accum_input: int | None = None
        self._accum_output: int | None = None
        self._accum_cached: int | None = None
        self._accum_cache_write: int | None = None
        self._attempts_with_input = 0
        self._attempts_with_output = 0
        self._attempts_with_cached = 0
        self._attempts_with_cache_write = 0
        self._exact_attempt_totals: list[int | None] = []
        self._attempt_total_bounds: list[int] = []

    def add_provider_response(self, value: Any) -> None:
        self._completed_attempts += 1
        if not isinstance(value, Mapping):
            self._exact_attempt_totals.append(None)
            self._attempt_total_bounds.append(0)
            return

        raw_usage = usage_from_transport(value)
        if not raw_usage.reported:
            self._exact_attempt_totals.append(None)
            self._attempt_total_bounds.append(0)
            return

        attempt = _compute_attempt_usage(raw_usage)
        if not attempt.reported:
            self._exact_attempt_totals.append(None)
            self._attempt_total_bounds.append(0)
            return

        self._reported_attempts += 1

        if attempt.input_bound is not None:
            self._accum_input = (
                attempt.input_bound
                if self._accum_input is None
                else self._accum_input + attempt.input_bound
            )
        if attempt.input_complete:
            self._attempts_with_input += 1

        if attempt.output_bound is not None:
            self._accum_output = (
                attempt.output_bound
                if self._accum_output is None
                else self._accum_output + attempt.output_bound
            )
        if attempt.output_complete:
            self._attempts_with_output += 1

        if attempt.cached_bound is not None:
            self._accum_cached = (
                attempt.cached_bound
                if self._accum_cached is None
                else self._accum_cached + attempt.cached_bound
            )
        if attempt.cached_complete:
            self._attempts_with_cached += 1

        if attempt.cache_write_bound is not None:
            self._accum_cache_write = (
                attempt.cache_write_bound
                if self._accum_cache_write is None
                else self._accum_cache_write + attempt.cache_write_bound
            )
        if attempt.cache_write_complete:
            self._attempts_with_cache_write += 1

        self._exact_attempt_totals.append(attempt.exact_total)
        self._attempt_total_bounds.append(attempt.total_bound)

    def build_coverage(self) -> TokenUsageCoverage | None:
        """Per-dimension completeness truth for the logical call."""
        if self._reported_attempts == 0:
            return None
        completed = max(self._completed_attempts, 1)

        input_complete = (
            self._attempts_with_input == completed
            if self._accum_input is not None
            else False
        )
        output_complete = (
            self._attempts_with_output == completed
            if self._accum_output is not None
            else False
        )
        cached_complete = (
            self._attempts_with_cached == completed
            if self._accum_cached is not None
            else False
        )

        # Logical Total is complete only if EVERY completed attempt had an exact total:
        total_complete = (
            len(self._exact_attempt_totals) == completed
            and all(t is not None for t in self._exact_attempt_totals)
        )

        return TokenUsageCoverage(
            input_tokens=input_complete,
            output_tokens=output_complete,
            cached_input_tokens=cached_complete,
            total_tokens=total_complete,
        )

    def build(self) -> TokenUsage | None:
        """Canonical usage of the logical call, or None when no attempt reported usage."""
        if self._reported_attempts == 0:
            return None

        cov = self.build_coverage()
        total: int | None = None
        if cov is not None and cov.total_tokens:
            total = sum(t for t in self._exact_attempt_totals if t is not None)
        else:
            bound = sum(self._attempt_total_bounds)
            if bound > 0 or any(t is not None for t in self._exact_attempt_totals):
                total = bound
            elif self._accum_input is not None or self._accum_output is not None:
                total = (self._accum_input or 0) + (self._accum_output or 0)
            else:
                total = None

        raw = TokenUsage(
            input_tokens=self._accum_input,
            output_tokens=self._accum_output,
            cached_input_tokens=self._accum_cached,
            total_tokens=total,
            cache_write_input_tokens=self._accum_cache_write,
        )
        canonical = raw.canonical(coverage=cov)
        return canonical if canonical.reported else None
