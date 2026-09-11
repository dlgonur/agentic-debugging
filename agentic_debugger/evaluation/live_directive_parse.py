"""Directive normalization, parsing, and qualification validation.

This module owns the wire-to-directive layer: provider envelope
resolution, the closed set of mechanical transport normalizations
(with byte-level provenance records), constraint validation, the
canonical ``_parse`` authority (fail-closed rejection categories),
and the provider-neutral synthetic qualification entry point.  Only
the policy-pinned normalizations exist; semantic repair is disabled."""

from __future__ import annotations

import hashlib, json, re

from dataclasses import field
from typing import Any, Mapping
from agentic_debugger.agent.controller_policy import (
    ActionName, ControllerBudgetLimits, ControllerBudgetState, HypothesisConfidence, HypothesisLedger, HypothesisStatus,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective, AddHypothesisDirective, ControllerSnapshot, ModelAdapterError, ModelDirective, ReviseHypothesisDirective, SetHypothesisStatusDirective, TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState, TRANSITION_GRAPH

from agentic_debugger.evaluation.live_contracts import DIRECTIVE_NORMALIZATION_POLICY_ID, DIRECTIVE_NORMALIZATION_SCHEMA_VERSION, DirectiveRejectionCategory, LIVE_DIRECTIVE_SCHEMA, LiveModelAdapterError, PROVIDER_COMPLETION_ENVELOPE_SCHEMA, redact_for_recording
from agentic_debugger.evaluation.live_directive_schema import _directive_schema_for_state, _rejected, _require_field, _validate_enum_constrained_arguments


def _resolve_raw_directive(response: Mapping[str, Any]) -> Any:
    """Unwrap the provider envelope without silently guessing when it is ambiguous.

    The wire convention is either a bare directive object or a
    ``{"usage": ..., "directive": {...}}`` wrapper.  A response carrying both a
    top-level ``kind`` and a nested ``directive`` mixes both conventions at
    once; guessing which one is meant would risk silently substituting a
    directive the model never actually returned, so it is rejected instead.
    """
    wrapped = "directive" in response
    inline = "kind" in response
    if wrapped and inline:
        raise _rejected(DirectiveRejectionCategory.AMBIGUOUS_ENVELOPE, "response has both a top-level 'kind' and a nested 'directive'")
    return response["directive"] if wrapped else response


def _normalize_redundant_trailing_brace(content: str) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Recover one mechanical trailing brace without changing directive data."""

    start = len(content) - len(content.lstrip(" \t\r\n"))
    if start >= len(content):
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(content, start)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    suffix = content[end:]
    if re.fullmatch(r"[ \t\r\n]*}[ \t\r\n]*", suffix) is None:
        return None
    normalized = content[:end]
    before = content.encode("utf-8")
    after = normalized.encode("utf-8")
    removed = suffix.encode("utf-8")
    before_record = _normalization_content_record(content)
    after_record = _normalization_content_record(normalized)
    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "redundant_trailing_closing_delimiter",
        "normalization_before": {
            "byte_length": len(before),
            **before_record,
        },
        "normalization_after": {
            "byte_length": len(after),
            **after_record,
        },
        "normalization_removed_prefix": None,
        "normalization_removed_suffix": {
            "byte_length": len(removed),
            "sha256": hashlib.sha256(removed).hexdigest(),
            "text": suffix,
        },
    }


def _normalization_content_record(content: str) -> dict[str, Any]:
    """Hash normalization content only when recording redaction is a no-op."""

    recorded = redact_for_recording(content)
    if recorded != content:
        return {"sha256": None, "raw_hash_withheld": True}
    return {
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "raw_hash_withheld": False,
    }


def _normalize_exact_json_markdown_fence(content: str) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Unwrap one exact whole-response lowercase-JSON Markdown fence.

    This is transport-envelope compatibility only.  The inner bytes must be a
    strict top-level JSON object and are neither stripped nor reserialized.
    The recovery is deliberately not composed with any other normalization.
    """

    match = re.fullmatch(
        r"(?P<outer_prefix>[ \t\r\n]*)```json\n(?P<inner>\{.*\})\n```(?P<outer_suffix>[ \t\r\n]*)",
        content,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    inner = match.group("inner")
    try:
        value = json.loads(inner)
        before = content.encode("utf-8")
        after = inner.encode("utf-8")
        removed_prefix_text = match.group("outer_prefix") + "```json\n"
        removed_suffix_text = "\n```" + match.group("outer_suffix")
        removed_prefix = removed_prefix_text.encode("utf-8")
        removed_suffix = removed_suffix_text.encode("utf-8")
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    before_record = _normalization_content_record(content)
    after_record = _normalization_content_record(inner)
    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "exact_json_markdown_fence",
        "normalization_before": {
            "byte_length": len(before),
            **before_record,
        },
        "normalization_after": {
            "byte_length": len(after),
            **after_record,
        },
        "normalization_removed_prefix": {
            "byte_length": len(removed_prefix),
            "sha256": hashlib.sha256(removed_prefix).hexdigest(),
            "text": removed_prefix_text,
        },
        "normalization_removed_suffix": {
            "byte_length": len(removed_suffix),
            "sha256": hashlib.sha256(removed_suffix).hexdigest(),
            "text": removed_suffix_text,
        },
    }


def _normalize_prose_wrapped_exact_json_markdown_fence(
    content: str,
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Recover one exact JSON fence surrounded by non-fenced model prose.

    Some providers place an otherwise valid directive after a short natural
    language explanation. Only the bytes inside one lowercase-JSON LF fence
    are considered; extra fences or malformed/non-object inner content remain
    rejected. The ignored prose is retained only through bounded provenance.
    """

    openings = list(re.finditer(r"```json\n", content))
    if len(openings) != 1:
        return None
    opening = openings[0]
    closing = re.search(r"\n```", content[opening.end():])
    if closing is None:
        return None
    close_start = opening.end() + closing.start()
    close_end = opening.end() + closing.end()
    prefix = content[:opening.start()]
    inner = content[opening.end():close_start]
    suffix = content[close_end:]
    if "```" in prefix or "```" in suffix:
        return None
    # Reject a bare four-backtick opener (the match would otherwise begin at
    # its second backtick), while tolerating a provider's single inline
    # backtick immediately before a prose-wrapped fence.
    if prefix.strip("` \t\r\n") == "" and prefix:
        return None
    if re.fullmatch(r"[ \t\r\n]*json[ \t\r\n]*", suffix):
        return None
    try:
        value = json.loads(inner)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    before = content.encode("utf-8")
    after = inner.encode("utf-8")
    removed_prefix_text = prefix + "```json\n"
    removed_suffix_text = "\n```" + suffix
    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "prose_wrapped_exact_json_markdown_fence",
        "normalization_before": {
            "byte_length": len(before),
            **_normalization_content_record(content),
        },
        "normalization_after": {
            "byte_length": len(after),
            **_normalization_content_record(inner),
        },
        "normalization_removed_prefix": {
            "byte_length": len(removed_prefix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_prefix_text.encode("utf-8")).hexdigest(),
            "text": removed_prefix_text,
        },
        "normalization_removed_suffix": {
            "byte_length": len(removed_suffix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_suffix_text.encode("utf-8")).hexdigest(),
            "text": removed_suffix_text,
        },
    }


def _normalize_prose_wrapped_exact_json_object(
    content: str,
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Recover one strict mapping surrounded by prose without braces.

    This handles providers that prepend or append a natural-language note but
    do not use a Markdown fence. The first JSON object must be the only brace
    pair-bearing region; that restriction prevents selecting one object from a
    prose stream containing multiple candidate directives.
    """

    # Markdown-fenced/unterminated forms have their own stricter branches;
    # keeping triple-backtick content out of this fallback preserves those
    # rejection and provenance semantics.
    if "```" in content:
        return None
    start = content.find("{")
    if start < 0:
        return None
    prefix = content[:start]
    if "{" in prefix or "}" in prefix:
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(content, start)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    suffix = content[end:]
    if "{" in suffix or "}" in suffix:
        return None
    if suffix and re.match(r"^[ \t\r\n]*[A-Za-z]", suffix) is None:
        return None
    inner = content[start:end]
    removed_prefix_text = prefix
    removed_suffix_text = suffix
    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "prose_wrapped_exact_json_object",
        "normalization_before": {
            "byte_length": len(content.encode("utf-8")),
            **_normalization_content_record(content),
        },
        "normalization_after": {
            "byte_length": len(inner.encode("utf-8")),
            **_normalization_content_record(inner),
        },
        "normalization_removed_prefix": {
            "byte_length": len(removed_prefix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_prefix_text.encode("utf-8")).hexdigest(),
            "text": removed_prefix_text,
        },
        "normalization_removed_suffix": {
            "byte_length": len(removed_suffix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_suffix_text.encode("utf-8")).hexdigest(),
            "text": removed_suffix_text,
        },
    }


def _normalize_unterminated_exact_json_markdown_fence(
    content: str,
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Recover an unclosed JSON fence only when its remainder is strict JSON.

    Some streaming providers return the opening Markdown fence and a complete
    JSON object but omit the closing fence. This remains unambiguous because
    the remainder must parse as exactly one top-level mapping with no prose.
    """

    match = re.fullmatch(
        r"(?P<outer_prefix>[ \t\r\n]*)```json\n(?P<inner>\{.*\})(?P<outer_suffix>[ \t\r\n]*)",
        content,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    inner = match.group("inner")
    try:
        value = json.loads(inner)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    before = content.encode("utf-8")
    after = inner.encode("utf-8")
    removed_prefix_text = match.group("outer_prefix") + "```json\n"
    removed_suffix_text = match.group("outer_suffix")
    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "unterminated_exact_json_markdown_fence",
        "normalization_before": {
            "byte_length": len(before),
            **_normalization_content_record(content),
        },
        "normalization_after": {
            "byte_length": len(after),
            **_normalization_content_record(inner),
        },
        "normalization_removed_prefix": {
            "byte_length": len(removed_prefix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_prefix_text.encode("utf-8")).hexdigest(),
            "text": removed_prefix_text,
        },
        "normalization_removed_suffix": {
            "byte_length": len(removed_suffix_text.encode("utf-8")),
            "sha256": hashlib.sha256(removed_suffix_text.encode("utf-8")).hexdigest(),
            "text": removed_suffix_text,
        },
    }


def _normalize_exact_json_fence_then_redundant_trailing_brace(
    content: str,
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Apply the one approved ordered composition with exact provenance.

    The outer envelope must satisfy the existing lowercase-JSON LF fence
    grammar. Its inner bytes must then satisfy the existing exactly-one
    redundant trailing brace rule. No other ordering or chaining is attempted.
    """

    match = re.fullmatch(
        r"(?P<outer_prefix>[ \t\r\n]*)```json\n(?P<inner>\{.*\})\n```(?P<outer_suffix>[ \t\r\n]*)",
        content,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    inner = match.group("inner")
    recovered = _normalize_redundant_trailing_brace(inner)
    if recovered is None:
        return None
    value, inner_provenance = recovered
    inner_removed_suffix = inner_provenance["normalization_removed_suffix"]["text"]
    normalized = inner[:-len(inner_removed_suffix)]
    removed_prefix_text = match.group("outer_prefix") + "```json\n"
    fence_suffix_text = "\n```" + match.group("outer_suffix")
    removed_suffix_text = inner_removed_suffix + fence_suffix_text

    def content_record(value: str) -> dict[str, Any]:
        encoded = value.encode("utf-8")
        return {
            "byte_length": len(encoded),
            **_normalization_content_record(value),
        }

    def removed_record(value: str) -> dict[str, Any]:
        encoded = value.encode("utf-8")
        return {
            "byte_length": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "text": value,
        }

    return value, {
        "directive_transport_normalized": True,
        "normalization_schema_version": DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
        "normalization_policy_id": DIRECTIVE_NORMALIZATION_POLICY_ID,
        "normalization_kind": "exact_json_markdown_fence_then_redundant_trailing_closing_delimiter",
        "normalization_before": content_record(content),
        "normalization_after": content_record(normalized),
        "normalization_removed_prefix": removed_record(removed_prefix_text),
        "normalization_removed_suffix": removed_record(removed_suffix_text),
        "normalization_steps": [
            {
                "kind": "exact_json_markdown_fence",
                "before": content_record(content),
                "after": content_record(inner),
                "removed_prefix": removed_record(removed_prefix_text),
                "removed_suffix": removed_record(fence_suffix_text),
            },
            {
                "kind": "redundant_trailing_closing_delimiter",
                "before": content_record(inner),
                "after": content_record(normalized),
                "removed_prefix": None,
                "removed_suffix": removed_record(inner_removed_suffix),
            },
        ],
    }


def _resolve_provider_directive(response: Mapping[str, Any]) -> tuple[Any, str | None, dict[str, Any] | None]:
    """Decode the provider-completion envelope, then use the canonical parser.

    ``directive_content`` is the only final assistant content form.  The
    command adapter deliberately does not JSON-decode or validate it.
    Existing deterministic transports may still return a direct mapping for
    compatibility with the offline test harness.
    """
    if "directive_content" not in response:
        return _resolve_raw_directive(response), None, None
    if response.get("provider_completion_schema_version") != PROVIDER_COMPLETION_ENVELOPE_SCHEMA:
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "provider completion envelope is unsupported", stage="extraction_failure", reason_code="invalid_completion_envelope")
    content = response.get("directive_content")
    if type(content) is not str:
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "provider completion content is not text", stage="extraction_failure", reason_code="content_not_text")
    if "directive" in response or "kind" in response:
        raise _rejected(DirectiveRejectionCategory.AMBIGUOUS_ENVELOPE, "provider completion mixes final content and directive fields", stage="envelope_failure", reason_code="mixed_completion_envelope", content=content)
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError):
        recovered = _normalize_redundant_trailing_brace(content)
        if recovered is None:
            recovered = _normalize_exact_json_markdown_fence(content)
        if recovered is None:
            recovered = _normalize_prose_wrapped_exact_json_markdown_fence(content)
        if recovered is None:
            recovered = _normalize_prose_wrapped_exact_json_object(content)
        if recovered is None:
            recovered = _normalize_unterminated_exact_json_markdown_fence(content)
        if recovered is None:
            recovered = _normalize_exact_json_fence_then_redundant_trailing_brace(content)
        if recovered is None:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "final content was not valid JSON", stage="json_failure", reason_code="invalid_json", content=content) from None
        value, normalization = recovered
        return value, content, normalization
    if not isinstance(value, Mapping):
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "final content was not a JSON object", stage="schema_failure", reason_code="directive_not_object", content=content)
    return value, content, None


def _validate_directive_constraints(
    value: Mapping[str, Any],
    directive_schema: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    """Enforce request-specific directive constraints before adaptation."""

    if directive_schema is None:
        return
    kind = value.get("kind")
    schema = directive_schema.get(kind) if type(kind) is str else None
    if not isinstance(schema, Mapping):
        return
    constraints = schema.get("constraints")
    if not isinstance(constraints, Mapping):
        return
    for field, constraint in constraints.items():
        if field not in value or not isinstance(constraint, Mapping):
            continue
        expected_type = constraint.get("type")
        field_value = value[field]
        valid_type = {
            "string": type(field_value) is str,
            "boolean": type(field_value) is bool,
            "array": type(field_value) is list,
            "object": type(field_value) is dict,
        }.get(expected_type, True)
        if not valid_type:
            raise _rejected(
                DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE,
                f"'{field}' failed the current directive constraint",
            )
        enum = constraint.get("enum")
        if isinstance(enum, list) and field_value not in enum:
            raise _rejected(
                DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE,
                f"'{field}' is outside the current directive constraint",
            )

def _parse(
    value: Any,
    snapshot: ControllerSnapshot,
    *,
    action_contracts: Mapping[str, Mapping[str, Any]] | None = None,
    legal_transition_targets: set[str] | None = None,
    directive_kinds: set[str] | None = None,
    directive_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> ModelDirective:
    if not isinstance(value, Mapping):
        raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "directive must be a JSON object")
    kind = value.get("kind")
    # ``kind`` is provider-controlled JSON.  Check its shape before using it
    # in set membership so arrays and objects cannot escape the bounded
    # provider-completed invalid-directive layer as unhashable values.
    if type(kind) is not str:
        raise _rejected(
            DirectiveRejectionCategory.MALFORMED_DIRECTIVE,
            "unrecognized or missing directive 'kind'",
        )
    known_kinds = set(LIVE_DIRECTIVE_SCHEMA)
    if kind not in known_kinds:
        raise _rejected(
            DirectiveRejectionCategory.MALFORMED_DIRECTIVE,
            "unrecognized or missing directive 'kind'",
        )
    if directive_kinds is not None and kind not in directive_kinds:
        raise _rejected(
            DirectiveRejectionCategory.ILLEGAL_ACTION,
            f"directive kind '{kind}' is not legal in state '{snapshot.state.value}'",
        )
    _validate_directive_constraints(value, directive_schema)
    if kind == "action":
        arguments = _require_field(value, "arguments")
        if not isinstance(arguments, Mapping):
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "'arguments' must be a JSON object")
        try:
            name = ActionName(_require_field(value, "name"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized action name") from None
        effective_actions = {
            ActionName(name) for name in action_contracts
        } if action_contracts is not None else set(snapshot.allowed_actions)
        if name not in effective_actions:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_ACTION, f"action '{name.value}' is not allowed in state '{snapshot.state.value}'")
        if action_contracts is not None:
            _validate_enum_constrained_arguments(name, arguments, action_contracts)
        try:
            return ActionDirective(name, dict(arguments))
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "action arguments failed validation") from None
    if kind == "transition":
        try:
            target = ControllerState(_require_field(value, "target_state"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized target_state") from None
        reason = _require_field(value, "reason")
        if legal_transition_targets is not None and target.value not in legal_transition_targets:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_TRANSITION, f"'{target.value}' is not reachable from '{snapshot.state.value}'")
        if legal_transition_targets is None and target not in TRANSITION_GRAPH[snapshot.state]:
            raise _rejected(DirectiveRejectionCategory.ILLEGAL_TRANSITION, f"'{target.value}' is not reachable from '{snapshot.state.value}'")
        try:
            return TransitionDirective(target, reason)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "'reason' failed validation") from None
    if kind in ("add_hypothesis", "revise_hypothesis"):
        hypothesis_id = _require_field(value, "hypothesis_id")
        statement = _require_field(value, "statement")
        try:
            confidence = HypothesisConfidence(_require_field(value, "confidence"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "'confidence' must be low, medium, or high") from None
        evidence_refs_raw = _require_field(value, "evidence_refs")
        requires_runtime_evidence = _require_field(value, "requires_runtime_evidence")
        if type(evidence_refs_raw) is not list:
            raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "'evidence_refs' must be a JSON array") from None
        evidence_refs = tuple(evidence_refs_raw)
        directive_cls = AddHypothesisDirective if kind == "add_hypothesis" else ReviseHypothesisDirective
        try:
            return directive_cls(hypothesis_id, statement, confidence, evidence_refs, requires_runtime_evidence)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "hypothesis fields failed validation") from None
    if kind == "set_hypothesis_status":
        hypothesis_id = _require_field(value, "hypothesis_id")
        try:
            status = HypothesisStatus(_require_field(value, "status"))
        except ValueError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "unrecognized hypothesis status") from None
        try:
            return SetHypothesisStatusDirective(hypothesis_id, status)
        except ModelAdapterError:
            raise _rejected(DirectiveRejectionCategory.INVALID_ARGUMENT_VALUE, "hypothesis status fields failed validation") from None
    raise _rejected(DirectiveRejectionCategory.MALFORMED_DIRECTIVE, "unrecognized or missing directive 'kind'")


def validate_synthetic_qualification_content(
    content: str,
    *,
    action_contracts: Mapping[str, Mapping[str, Any]],
    directive_kinds: set[str] | None = None,
    legal_transition_targets: set[str] | None = None,
    directive_schema: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate qualification content with the live parser authority.

    This is deliberately a provider-neutral, Level-32-independent entry point
    for transport qualification.  It performs the same envelope extraction,
    accepted normalization, and semantic/action validation used by live
    treatments, but supplies a minimal synthetic ``Reproduce`` snapshot and
    caller-provided synthetic action contract.
    """

    if type(content) is not str:
        return {
            "schema_version": "transport-qualification-directive-v1",
            "directive_protocol_ok": False,
            "category": "DIRECTIVE_SEMANTIC_REJECTED",
            "reason_code": "content_not_text",
            "stage": "extraction_failure",
        }
    try:
        value, _final_content, normalization = _resolve_provider_directive(
            {
                "provider_completion_schema_version": PROVIDER_COMPLETION_ENVELOPE_SCHEMA,
                "directive_content": content,
            }
        )
        snapshot = ControllerSnapshot(
            run_id="qualification",
            task_id="qualify-synthetic",
            state=ControllerState.REPRODUCE,
            model_call_index=0,
            budget_limits=ControllerBudgetLimits(
                max_patch_attempts=1,
                max_test_runs=1,
                max_pdb_observations=1,
            ),
            budget_state=ControllerBudgetState(),
            hypotheses=HypothesisLedger(),
        )
        directive = _parse(
            value,
            snapshot,
            action_contracts=action_contracts,
            legal_transition_targets=(
                set() if legal_transition_targets is None else legal_transition_targets
            ),
            directive_kinds=(
                {"action"} if directive_kinds is None else directive_kinds
            ),
            directive_schema=(
                _directive_schema_for_state(ControllerState.REPRODUCE)
                if directive_schema is None
                else directive_schema
            ),
        )
        result: dict[str, Any] = {
            "schema_version": "transport-qualification-directive-v1",
            "directive_protocol_ok": True,
            "category": "DIRECTIVE_PROTOCOL_VERIFIED",
            "directive_kind": directive.kind.value,
            "normalization_applied": normalization is not None,
        }
        if normalization is not None:
            result["normalization_kind"] = normalization.get("normalization_kind")
        if isinstance(directive, ActionDirective):
            result["action_name"] = directive.name.value
        return result
    except LiveModelAdapterError as exc:
        category = (
            "DIRECTIVE_INVALID_JSON"
            if exc.reason_code == "invalid_json"
            else "DIRECTIVE_SEMANTIC_REJECTED"
        )
        return {
            "schema_version": "transport-qualification-directive-v1",
            "directive_protocol_ok": False,
            "category": category,
            "reason_code": exc.reason_code,
            "stage": exc.stage,
        }
