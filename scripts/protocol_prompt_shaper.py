"""Provider-neutral protocol-1.3 prompt-shaping authority.

One canonical model-facing contract consumed by every model transport
(the Ollama Cloud command adapter and the provider direct-API adapter).
The semantics are the accepted mature ladder-facing shaping: a real
system-role instruction, the exact legal top-level directive forms, and
request-specific legal action / transition / hypothesis / diagnosis
representations derived only from the current public protocol request.

The module is deliberately self-contained (no repository-package imports):
command adapters run as bare child processes whose only guaranteed import
root is this scripts directory.  Every value exposed to the model derives
from the request itself; nothing is fabricated and no fixture oracle value
appears here.

Adapters keep their own public-request byte ceilings by passing
``max_request_bytes``; the default is the mature ladder ceiling.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

MAX_PUBLIC_REQUEST_BYTES = 32_768

PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="

#: Bound on the diagnosis observed-values example derived from the current
#: public ``get_frame_locals`` observation.  The full payload remains in the
#: canonical request; this bounds only the duplicated example.
_MAX_DIAGNOSIS_OBSERVED_EXAMPLES = 6


class ProtocolPromptError(ValueError):
    """Neutral bounded failure; adapters map it to their typed errors."""

    def __init__(self, message: str, *, kind: str = "invalid_request") -> None:
        super().__init__(message)
        self.kind = kind


SYSTEM_PROMPT = (
    "You are the debugging decision model for Local Application V1.\n"
    "Return exactly one legal JSON protocol directive.\n"
    "Output exactly one JSON object. Do not output Markdown, code fences, prose, explanations, or free-form text before or after it.\n"
    "Use only the exact protocol field names. Never invent semantic aliases.\n"
    "Never combine an action and a transition into one object. Choose one legal directive only.\n"
    "Do not directly invoke tools or functions, and do not perform filesystem, command, or repository operations.\n"
    "When the supplied allowed_actions and action_contracts permit an action, you may and should return that legal action directive.\n"
    "Local Application performs every actual action described by an accepted directive.\n"
    "The top-level field identifying directive type is always \"kind\".\n"
    "Do not use top-level keys named action, payload, or transition.\n"
    "\n"
    "Exact legal top-level forms. The first key is always \"kind\".\n"
    "Action: {\"kind\":\"action\",\"name\":\"<allowed action>\",\"arguments\":{...}}\n"
    "kind must literally be \"action\". name must come from controller.allowed_actions. arguments must satisfy that action's supplied action_contracts. Do not use top-level keys named action or payload.\n"
    "Transition: {\"kind\":\"transition\",\"target_state\":\"<legal target>\",\"reason\":\"<bounded reason>\"}\n"
    "kind must literally be \"transition\". target_state must come from controller.legal_transition_targets. Do not use a top-level key named transition.\n"
    "add_hypothesis and revise_hypothesis use exactly these fields: \"kind\", \"hypothesis_id\", \"statement\", \"confidence\", \"evidence_refs\", and \"requires_runtime_evidence\".\n"
    "For either hypothesis kind, copy the current user message's Legal hypothesis representation and obey every current directive_schema constraint; especially do not guess or invert the required requires_runtime_evidence boolean.\n"
    "set_hypothesis_status: {\"kind\":\"set_hypothesis_status\",\"hypothesis_id\":\"<id>\",\"status\":\"supported|rejected|discarded\"}\n"
    "confidence must be exactly one of low, medium, high. status must be exactly one of supported, rejected, discarded. evidence_refs is a list of strings. requires_runtime_evidence is a boolean."
)


#: Canonical top-level directive field order per kind.  Consuming adapters
#: keep their own validator field sets and verify the shared prompt still
#: teaches exactly those names.
DIRECTIVE_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "action": ("kind", "name", "arguments"),
    "transition": ("kind", "target_state", "reason"),
    "add_hypothesis": (
        "kind",
        "hypothesis_id",
        "statement",
        "confidence",
        "evidence_refs",
        "requires_runtime_evidence",
    ),
    "revise_hypothesis": (
        "kind",
        "hypothesis_id",
        "statement",
        "confidence",
        "evidence_refs",
        "requires_runtime_evidence",
    ),
    "set_hypothesis_status": ("kind", "hypothesis_id", "status"),
}


def directive_fields_match_validator(
    top_level_fields: Mapping[str, frozenset],
) -> bool:
    """The shared field order must equal the consuming validator's field sets."""

    if set(DIRECTIVE_FIELD_ORDER) != set(top_level_fields):
        return False
    return all(
        frozenset(fields) == frozenset(top_level_fields[kind])
        for kind, fields in DIRECTIVE_FIELD_ORDER.items()
    )


def build_system_instructions(request: Mapping[str, Any]) -> str:
    """The provider-neutral system-role instruction (request-independent)."""

    return SYSTEM_PROMPT


def canonical_public_request(
    request: Mapping[str, Any],
    *,
    max_request_bytes: int = MAX_PUBLIC_REQUEST_BYTES,
) -> str:
    if not isinstance(request, Mapping):
        raise ProtocolPromptError("protocol request must be an object")
    try:
        canonical = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        size = len(canonical.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise ProtocolPromptError("protocol request is not strict JSON") from None
    if size > max_request_bytes:
        raise ProtocolPromptError(
            "canonical public request exceeds the Local Application ceiling",
            kind="request_too_large",
        )
    return canonical


def _directive_kinds(request: Mapping[str, Any]) -> list[str]:
    kinds = request.get("directive_schema")
    if isinstance(kinds, Mapping):
        kinds = list(kinds)
    if not isinstance(kinds, list):
        return []
    return [kind for kind in kinds if type(kind) is str]


def _illustrative_argument_value(field: str, spec: Mapping[str, Any] | None) -> Any:
    if isinstance(spec, Mapping):
        if "example" in spec:
            return spec["example"]
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        type_name = spec.get("type")
        if type_name == "boolean":
            return True
        if type_name == "integer":
            minimum = spec.get("minimum")
            if type(minimum) is int and not isinstance(minimum, bool):
                return minimum
            return 0
        if type_name == "number":
            minimum = spec.get("minimum")
            if type(minimum) in (int, float) and not isinstance(minimum, bool):
                return minimum
            return 0
        if type_name == "array":
            return []
        if type_name == "object":
            return {}
        if type_name == "null":
            return None
    return f"<{field}>"


NEUTRAL_UNIFIED_DIFF_EXAMPLE = (
    "--- a/example.py\n"
    "+++ b/example.py\n"
    "@@ -1,3 +1,4 @@\n"
    " keep = True\n"
    "-old_a = 1\n"
    "-old_b = 2\n"
    "+new_a = 1\n"
    "+new_b = 2\n"
    "+new_c = 3\n"
)

OLD_COUNT_FORMULA = (
    'OLD_COUNT = number of lines beginning with " " + number of lines beginning with "-"'
)
NEW_COUNT_FORMULA = (
    'NEW_COUNT = number of lines beginning with " " + number of lines beginning with "+"'
)

APPLY_PATCH_DIRECTIVE_SHAPE = (
    '{"kind":"action","name":"apply_patch","arguments":{"patch":"..."}}'
)


def _syntax_check_advertises_path(contracts: Any) -> bool:
    if not isinstance(contracts, Mapping):
        return False
    contract = contracts.get("syntax_check")
    if not isinstance(contract, Mapping):
        return False
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    return "path" in properties or "paths" in properties


def _patch_budget_remaining(controller: Mapping[str, Any]) -> str | None:
    limits = controller.get("budget_limits")
    state = controller.get("budget_state")
    if not isinstance(limits, Mapping) or not isinstance(state, Mapping):
        return None
    maximum = limits.get("max_patch_attempts")
    used = state.get("patch_attempts")
    if (
        type(maximum) is not int
        or isinstance(maximum, bool)
        or type(used) is not int
        or isinstance(used, bool)
        or maximum <= 0
        or used < 0
    ):
        return None
    remaining = maximum - used
    if remaining > 0:
        return f"Patch-attempt budget remaining for this request: {remaining} of {maximum}."
    return "Patch-attempt budget for this request is exhausted."


def _public_observations(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Unique public observations of the current request, most recent last.

    History entries and the controller's current ``last_observation`` are the
    complete public runtime evidence a transport request carries.
    """

    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    candidates: list[Any] = [
        entry.get("last_observation")
        for entry in request.get("history", [])
        if isinstance(entry, Mapping)
    ]
    candidates.append(controller.get("last_observation"))
    by_id: dict[str, Mapping[str, Any]] = {}
    for observation in candidates:
        if (
            isinstance(observation, Mapping)
            and type(observation.get("observation_id")) is str
        ):
            by_id[observation["observation_id"]] = observation
    return list(by_id.values())


def _latest_ok_observation(
    observations: list[Mapping[str, Any]], names: tuple[str, ...]
) -> Mapping[str, Any] | None:
    matches = [
        observation
        for observation in observations
        if observation.get("name") in names and observation.get("status") == "ok"
    ]
    return matches[-1] if matches else None


def _public_breakpoint_source(request: Mapping[str, Any]) -> tuple[str, int, str] | None:
    """Return a source line already visible in the current exact-PDB history."""

    breakpoint_line: int | None = None
    source_lines: dict[tuple[str, int], str] = {}
    for observation in _public_observations(request):
        payload = observation.get("payload")
        if not isinstance(payload, Mapping):
            continue
        proof = payload.get("proof")
        if isinstance(proof, Mapping) and type(proof.get("breakpoint_line")) is int:
            breakpoint_line = proof["breakpoint_line"]
        lines = payload.get("lines")
        if isinstance(lines, list):
            for entry in lines:
                if not isinstance(entry, Mapping):
                    continue
                path = entry.get("path")
                number = entry.get("line_number")
                text = entry.get("text")
                if type(path) is str and type(number) is int and type(text) is str:
                    source_lines[(path, number)] = text
    if breakpoint_line is None:
        return None
    matches = [
        (path, number, text)
        for (path, number), text in source_lines.items()
        if number == breakpoint_line
    ]
    return matches[0] if len(matches) == 1 else None


def build_apply_patch_guidance(request: Mapping[str, Any]) -> str:
    """PatchManager-derived apply_patch format and recovery rules."""

    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    contracts = request.get("action_contracts")
    lines = [
        "apply_patch arguments.patch must be a complete unified diff accepted by Local Application's PatchManager. The Level-32 operator derives the strict official Git artifact from the accepted workspace state.",
        "File headers must contain both lines, in this order:",
        "--- a/<relative-path>",
        "+++ b/<same-relative-path>",
        "Every hunk requires a complete numeric header of the form:",
        "@@ -OLD_START,OLD_COUNT +NEW_START,NEW_COUNT @@",
        "OLD_START and NEW_START are 1-based line positions.",
        "Always emit the complete form. Never emit bare @@.",
        "Never leave symbolic placeholders such as OLD_COUNT in the actual patch.",
        "After composing each hunk body, count prefixes mechanically before returning the JSON directive:",
        OLD_COUNT_FORMULA,
        NEW_COUNT_FORMULA,
        'Lines beginning with "-" do not count toward NEW_COUNT.',
        'Lines beginning with "+" do not count toward OLD_COUNT.',
        'Context lines beginning with exactly one space count toward both OLD_COUNT and NEW_COUNT.',
        "Hunk counts must exactly match the hunk body.",
        "If the header counts do not equal the body counts, correct the header before output.",
        "Prefer the smallest valid hunk that uniquely expresses the edit. Include unchanged context lines when available; do not rely on manual hunk serialization for official evaluation.",
        "Hunk body prefixes are significant: one leading space for unchanged/context, - for removed, + for added.",
        "Use repository-relative paths only.",
        "Do not wrap the patch string in Markdown fences.",
        "The complete response is JSON: encode every patch line break as the JSON escape \\n inside arguments.patch; never place a literal unescaped newline inside that quoted JSON string.",
        "Do not include unsupported Git metadata such as diff --git, new file, deleted file, rename, or copy lines.",
        f"The entire patch remains the value of {APPLY_PATCH_DIRECTIVE_SHAPE}",
        "Neutral arithmetic example. For this body, OLD_COUNT = 1 context + 2 removed = 3 and NEW_COUNT = 1 context + 3 added = 4:",
        NEUTRAL_UNIFIED_DIFF_EXAMPLE.rstrip("\n"),
        "Before emitting the JSON, verify:",
        "1. --- and +++ headers both exist and refer to the same repository-relative path.",
        "2. Every hunk header contains four numeric values.",
        "3. Count every hunk body line by prefix.",
        "4. Recompute OLD_COUNT from context + removed.",
        "5. Recompute NEW_COUNT from context + added.",
        "6. Header counts exactly equal those totals.",
        '7. Every hunk body line starts with " ", "-", or "+".',
        "8. No Markdown fences or unsupported Git metadata.",
        f"9. The complete patch is inside {APPLY_PATCH_DIRECTIVE_SHAPE}",
        "A rejected apply_patch does not create an active patch and does not mutate the workspace.",
        "After a rejected patch, do not call revert_patch merely to undo that rejected patch.",
    ]
    breakpoint_source = _public_breakpoint_source(request)
    if breakpoint_source is not None:
        path, line_number, old_line = breakpoint_source
        lines.extend(
            [
                f"Current public PDB/source evidence binds the diagnosed line to {path}:{line_number}.",
                "Use that source line as the removed line in a normal context-bearing hunk when possible; the operator will serialize the accepted workspace state for official Git evaluation.",
            ]
        )
    if _syntax_check_advertises_path(contracts):
        lines.append(
            "Do not call patch-dependent syntax_check without an active successfully applied patch unless using the advertised path argument."
        )
    else:
        lines.append(
            "Do not call patch-dependent syntax_check without an active successfully applied patch."
        )
    lines.append(
        "If apply_patch remains legal and patch-attempt budget remains, correct the patch format or content and submit a new valid apply_patch."
    )
    last_obs = controller.get("last_observation")
    if isinstance(last_obs, Mapping) and last_obs.get("name") == "apply_patch":
        obs_status = last_obs.get("status")
        if obs_status in ("error", "rejected"):
            obs_payload = last_obs.get("payload")
            patch_failure = (
                obs_payload.get("patch_failure")
                if isinstance(obs_payload, Mapping) and isinstance(obs_payload.get("patch_failure"), Mapping)
                else {}
            )
            summary_text = (
                (obs_payload.get("diagnostic") if isinstance(obs_payload, Mapping) else None)
                or last_obs.get("summary")
            )
            source_win = patch_failure.get("current_source_window")
            if summary_text or source_win:
                failure_lines = ["PREVIOUS APPLY_PATCH ATTEMPT FAILED:"]
                if summary_text and isinstance(summary_text, str):
                    failure_lines.append(f"Diagnostic: {summary_text.strip()}")
                if patch_failure.get("path"):
                    failure_lines.append(f"Target file: {patch_failure['path']}")
                if patch_failure.get("line_number"):
                    failure_lines.append(f"Target line: {patch_failure['line_number']}")
                if source_win and isinstance(source_win, str):
                    failure_lines.extend([
                        "Current source around target:",
                        f"{source_win.strip()}",
                    ])
                failure_lines.append(
                    "Carefully examine the current source context and revise your proposed patch hunk to match the actual source."
                )
                lines.extend(failure_lines)
    lines.append(
        "After a patch is successfully applied, use the legal validation lifecycle exposed by the current controller and tools, including revert_patch only for an active successfully applied patch."
    )
    budget = _patch_budget_remaining(controller)
    if budget is not None:
        lines.append(budget)
    return "\n".join(lines)


def illustrative_action_directive(name: str, contracts: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if isinstance(contracts, Mapping):
        contract = contracts.get(name)
        if isinstance(contract, Mapping):
            properties = contract.get("properties")
            if not isinstance(properties, Mapping):
                properties = contract
            required = contract.get("required")
            if isinstance(required, list):
                for field in required:
                    if type(field) is not str:
                        continue
                    spec = properties.get(field) if isinstance(properties, Mapping) else None
                    arguments[field] = _illustrative_argument_value(
                        field,
                        spec if isinstance(spec, Mapping) else None,
                    )
    return {"kind": "action", "name": name, "arguments": arguments}


def _diagnosis_example(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """The request-specific legal diagnosis shape from public evidence only.

    Every concrete value (hypothesis id, diagnosed file/symbol, evidence
    observation ids, observed local values, confidence enum) is copied from
    the current public controller/history/contract fields; a structural
    placeholder is used only where the public evidence is not (yet)
    available.  Nothing is invented.
    """

    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        return None
    contracts = request.get("action_contracts")
    contract = (
        contracts.get("express_root_cause_hypothesis")
        if isinstance(contracts, Mapping)
        else None
    )
    if not isinstance(contract, Mapping):
        return None
    properties = contract.get("properties")
    if not isinstance(properties, Mapping):
        properties = contract
    required = contract.get("required")
    if not isinstance(required, list) or not all(type(field) is str for field in required):
        return None

    def constrained_value(field: str, fallback: Any) -> Any:
        spec = properties.get(field)
        if isinstance(spec, Mapping) and "example" in spec:
            return spec["example"]
        enum = spec.get("enum") if isinstance(spec, Mapping) else None
        return enum[0] if isinstance(enum, list) and enum else fallback

    placeholders = {
        "hypothesis_id": "<current active hypothesis_id>",
        "statement": "<your root-cause statement>",
        "target_file": "<diagnosed file>",
        "target_symbol": "<diagnosed function>",
        "evidence_refs": ["<current observation_id>"],
        "observed_values": {"<local name>": "<exact observed value>"},
    }
    arguments: dict[str, Any] = {
        field: (
            constrained_value(field, placeholders.get(field))
            if field == "confidence"
            else placeholders[field]
        )
        for field in required
        if field in placeholders or field == "confidence"
    }

    hypotheses = controller.get("hypotheses")
    active = (
        [
            item
            for item in hypotheses
            if isinstance(item, Mapping)
            and item.get("status") == "active"
            and type(item.get("hypothesis_id")) is str
        ]
        if isinstance(hypotheses, list)
        else []
    )
    if "hypothesis_id" in arguments and len(active) == 1:
        arguments["hypothesis_id"] = active[0]["hypothesis_id"]

    observations = _public_observations(request)
    start = _latest_ok_observation(observations, ("start_pdb_session",))
    start_payload = start.get("payload") if isinstance(start, Mapping) else None
    proof = (
        start_payload.get("proof")
        if isinstance(start_payload, Mapping)
        else None
    )
    proof = proof if isinstance(proof, Mapping) else {}
    if "target_file" in arguments and type(proof.get("production_file")) is str:
        arguments["target_file"] = proof["production_file"]
    if "target_symbol" in arguments and type(proof.get("production_frame")) is str:
        arguments["target_symbol"] = proof["production_frame"]

    stack = _latest_ok_observation(observations, ("get_stack_summary",))
    locals_observation = _latest_ok_observation(observations, ("get_frame_locals",))
    control = _latest_ok_observation(
        observations, ("next_pdb_session", "step_pdb_session")
    )
    if (
        "evidence_refs" in arguments
        and start is not None
        and stack is not None
        and locals_observation is not None
    ):
        refs = [
            observation["observation_id"]
            for observation in (start, stack, locals_observation, control)
            if observation is not None
        ]
        if refs:
            arguments["evidence_refs"] = refs
    if "observed_values" in arguments and locals_observation is not None:
        local_entries = locals_observation.get("payload")
        local_entries = (
            local_entries.get("locals")
            if isinstance(local_entries, Mapping)
            else None
        )
        visible = (
            entry
            for entry in local_entries
            if isinstance(entry, Mapping) and type(entry.get("name")) is str
        ) if isinstance(local_entries, list) else iter(())
        observed: dict[str, Any] = {}
        for entry in visible:
            if len(observed) >= _MAX_DIAGNOSIS_OBSERVED_EXAMPLES:
                break
            observed[entry["name"]] = entry.get("value")
        if observed:
            arguments["observed_values"] = observed

    return {
        "kind": "action",
        "name": "express_root_cause_hypothesis",
        "arguments": arguments,
    }


def build_request_guidance(request: Mapping[str, Any]) -> str:
    """Request-specific legal shapes derived from the current protocol request."""

    kinds = set(_directive_kinds(request))
    controller = request.get("controller")
    if not isinstance(controller, Mapping):
        controller = {}
    lines = [
        "Current request legal decision surface:",
        "Return exactly one JSON object using only the exact protocol field names.",
        "Do not invent keys named action, payload, or transition.",
        "Do not combine an action and a transition.",
    ]
    proof_gate = request.get("proof_gate")
    if isinstance(proof_gate, Mapping):
        next_actions = proof_gate.get("next_required_actions")
        if isinstance(next_actions, list) and all(type(item) is str for item in next_actions):
            lines.append(
                "Exact-proof next required actions: "
                + (", ".join(next_actions) if next_actions else "none; use a legal transition")
                + "."
            )
    directive_schema = request.get("directive_schema")
    if isinstance(directive_schema, Mapping):
        for hypothesis_kind in ("add_hypothesis", "revise_hypothesis"):
            if hypothesis_kind not in kinds:
                continue
            schema = directive_schema.get(hypothesis_kind)
            constraints = schema.get("constraints") if isinstance(schema, Mapping) else None
            runtime_constraint = (
                constraints.get("requires_runtime_evidence")
                if isinstance(constraints, Mapping)
                else None
            )
            runtime_values = (
                runtime_constraint.get("enum")
                if isinstance(runtime_constraint, Mapping)
                else None
            )
            runtime_value = (
                runtime_values[0]
                if isinstance(runtime_values, list)
                and len(runtime_values) == 1
                and type(runtime_values[0]) is bool
                else False
            )
            def constrained_value(field: str, fallback: Any) -> Any:
                constraint = constraints.get(field) if isinstance(constraints, Mapping) else None
                if isinstance(constraint, Mapping) and "example" in constraint:
                    return constraint["example"]
                values = constraint.get("enum") if isinstance(constraint, Mapping) else None
                return values[0] if isinstance(values, list) and len(values) == 1 else fallback

            example = {
                "kind": hypothesis_kind,
                "hypothesis_id": constrained_value("hypothesis_id", "hypothesis-1"),
                "statement": "bounded hypothesis",
                "confidence": constrained_value("confidence", "low"),
                "evidence_refs": constrained_value("evidence_refs", []),
                "requires_runtime_evidence": runtime_value,
            }
            lines.append(
                "Legal hypothesis representation: "
                + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            )
            if hypothesis_kind == "revise_hypothesis":
                lines.append(
                    "For revise_hypothesis, replace evidence_refs with actual observation_id values from current history."
                )
    allowed = controller.get("allowed_actions")
    contracts = request.get("action_contracts")
    diagnosis_advertised = False
    if "action" in kinds and isinstance(allowed, list):
        for name in allowed:
            if type(name) is not str or not name:
                continue
            example = illustrative_action_directive(name, contracts)
            lines.append(
                "Legal action representation: "
                + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
            )
            if any(
                type(value) is str and value.startswith("<") and value.endswith(">")
                for value in example.get("arguments", {}).values()
            ):
                lines.append(
                    "Every angle-bracket value in that action shape is structural; replace it with a substantive current value and never copy the placeholder literally."
                )
            if name == "start_pdb_session":
                lines.append(
                    "The shown breakpoint number is only a shape. Replace it with a visible executable target-function line; not def/import/module code."
                )
            if name == "apply_patch":
                lines.append(build_apply_patch_guidance(request))
            if name == "express_root_cause_hypothesis":
                diagnosis_advertised = True
    if diagnosis_advertised and isinstance(allowed, list):
        diagnosis = _diagnosis_example(request)
        if diagnosis is not None:
            lines.append(
                "Current diagnosis decision (express_root_cause_hypothesis): this request's exact current legal shape, with every concrete value copied from the current public controller, contracts, and PDB history evidence above; replace the angle-bracket placeholders with your own substantive current values."
            )
            lines.append(
                "Current diagnosis legal representation: "
                + json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))
            )
            if "evidence_refs" in diagnosis.get("arguments", {}):
                lines.append(
                    "For express_root_cause_hypothesis, evidence_refs must be actual observation_id values from current history and every observed_values entry must be an exact name/value pair from the current get_frame_locals observation."
                )
    targets = controller.get("legal_transition_targets")
    if "transition" in kinds and isinstance(targets, list) and targets:
        legal_targets = [target for target in targets if type(target) is str]
        if len(legal_targets) == 1:
            lines.append(
                "Legal transition representation: "
                + json.dumps(
                    {
                        "kind": "transition",
                        "target_state": legal_targets[0],
                        "reason": "advance using the sole legal target",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        elif legal_targets:
            lines.append(
                "Legal transition representation: "
                '{"kind":"transition","target_state":"<one of '
                + ", ".join(legal_targets)
                + '>","reason":"<bounded reason>"}'
            )
    return "\n".join(lines)


def build_user_protocol_message(
    request: Mapping[str, Any],
    *,
    max_request_bytes: int = MAX_PUBLIC_REQUEST_BYTES,
) -> str:
    """The user-role body: request-specific guidance plus the canonical request."""

    canonical = canonical_public_request(request, max_request_bytes=max_request_bytes)
    return (
        f"{build_request_guidance(request)}\n\n"
        f"{PUBLIC_REQUEST_START}\n{canonical}\n{PUBLIC_REQUEST_END}"
    )


def build_chat_messages(
    request: Mapping[str, Any],
    *,
    max_request_bytes: int = MAX_PUBLIC_REQUEST_BYTES,
) -> list[dict[str, str]]:
    """The canonical system/user message pair every transport adapts."""

    return [
        {
            "role": "system",
            "content": build_system_instructions(request),
        },
        {
            "role": "user",
            "content": build_user_protocol_message(
                request, max_request_bytes=max_request_bytes
            ),
        },
    ]
