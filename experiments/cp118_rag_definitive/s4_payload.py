"""S4 — frozen v1.2.1 one-shot payload assembly with the additive RAG block.

The primary S4 treatment keeps the frozen v1.2.1 one-shot protocol
(``experiments/raw-pilot-v1.1/scripts/RAW_C9_5Model_40Task_Protocol_v1_2_1_GPU_GENERATE_v1.py``)
byte-compatible and changes exactly one frozen variable: the model-facing
request additionally carries the frozen ``RagContext.to_request_mapping()``
``retrieved_context`` block.

* ``OUTPUT_REQUIREMENTS_V121`` and ``build_v12_payload`` are verbatim copies
  of the frozen script (a drift test compares them against the frozen
  source file at runtime).
* The ``RETRIEVED_CONTEXT`` block is inserted deterministically between the
  ``FAILING_TEST_OUTPUT`` section and the ``OUTPUT_REQUIREMENTS`` section,
  serialized with the project canonical JSON rules
  (``agentic_debugger.rag.schema.canonical_json``).
* Prompt budgets (Amendment 2): the frozen one-shot prompt budget is
  ``max_prompt_tokens = 24_576`` tokens and ``max_new_tokens = 4_096``.
  ``PUBLIC_REQUEST_BYTE_BUDGET = 20_000`` is scoped to the
  ``LiveModelAdapter`` agentic public-request path
  (``agentic_debugger/evaluation/live.py``) and is NOT the one-shot prompt
  limit; it is recorded here only for provenance.  The S4 runner records
  independently: base prompt bytes, retrieved-context bytes, assembled
  prompt bytes, assembled prompt tokens, RagContext truncation, and the
  protocol ``max_prompt_tokens`` truncation, and fails closed if the frozen
  constraints cannot coexist for a task.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple

from agentic_debugger.rag.schema import canonical_json

# ---------------------------------------------------------------------------
# Frozen v1.2.1 protocol constants (verbatim from the frozen GPU script).
# ---------------------------------------------------------------------------

MAX_NEW_TOKENS = 4096
MAX_PROMPT_TOKENS = 24_576
PROTOCOL_VERSION = "v1.2.1"

# Verbatim copy of OUTPUT_REQUIREMENTS_V121 from
# RAW_C9_5Model_40Task_Protocol_v1_2_1_GPU_GENERATE_v1.py (lines 56-72).
OUTPUT_REQUIREMENTS_V121 = """Return exactly these four sections in this order:
PATCH
<raw unified diff>
FILES
<one repository-relative path per line>
SYMBOLS
<path>::<symbol, one per line>
ROOT_CAUSE
<brief causal explanation, maximum 150 words>

PATCH rules:
- PATCH must come first.
- Emit a real unified diff with explicit --- and +++ repository-relative paths and at least one @@ hunk.
- A leading diff --git line is allowed but not required.
- Do not output a complete replacement source file instead of a diff.

No Markdown fences. No prose before PATCH. No prose after ROOT_CAUSE."""

#: Frozen section markers of the v1.1 payload (v1.2.1 replaces the block
#: after the OUTPUT_REQUIREMENTS marker).
_V12_MARKER = "\n\nOUTPUT_REQUIREMENTS\n"

#: S4 protocol: the retrieved-context block marker (deterministic insertion
#: point is before the OUTPUT_REQUIREMENTS marker, i.e. after the frozen
#: FAILING_TEST_OUTPUT section).
RETRIEVED_CONTEXT_MARKER = "\n\nRETRIEVED_CONTEXT\n"


class PayloadError(RuntimeError):
    """Raised when a frozen payload cannot be assembled safely."""


def build_v12_payload(v11_payload: str) -> str:
    """Verbatim frozen v1.2.1 payload revision logic."""

    if v11_payload.count(_V12_MARKER) != 1:
        raise PayloadError(
            "Frozen payload does not contain exactly one OUTPUT_REQUIREMENTS "
            f"marker; count={v11_payload.count(_V12_MARKER)}"
        )
    prefix, _old = v11_payload.split(_V12_MARKER, 1)
    return prefix + _V12_MARKER + OUTPUT_REQUIREMENTS_V121


def parse_payload_sections(payload: str) -> Dict[str, str]:
    """Deterministic section split of a v1.1/v1.2.1 payload.

    Sections are introduced by an exact marker line at column 0
    (``TASK``, ``PROBLEM``, ``REPOSITORY_TREE``, ``SOURCE_FILES``,
    ``FAILING_TEST_OUTPUT``, ``OUTPUT_REQUIREMENTS``).  Returns
    ``{section_name: text}``; raises ``PayloadError`` on any marker that
    appears more than once or missing required sections.
    """

    import re

    markers = (
        "TASK",
        "PROBLEM",
        "REPOSITORY_TREE",
        "SOURCE_FILES",
        "FAILING_TEST_OUTPUT",
        "OUTPUT_REQUIREMENTS",
    )
    positions: List[Tuple[int, str]] = []
    for name in markers:
        matches = list(re.finditer(rf"(?m)^{re.escape(name)}\s*$", payload))
        if len(matches) != 1:
            raise PayloadError(
                f"payload section marker {name!r} found {len(matches)} times"
            )
        positions.append((matches[0].start(), name))
    positions.sort()
    sections: Dict[str, str] = {}
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(payload)
        text = payload[start:end]
        # Strip the marker line itself.
        body = text.split("\n", 1)[1] if "\n" in text else ""
        sections[name] = body.strip("\n")
    return sections


def assemble_rag_payload(v12_payload: str, rag_mapping: Dict[str, Any]) -> str:
    """Assemble the S4 one-shot request: frozen v1.2.1 payload plus the
    additive canonical ``retrieved_context`` block.

    The block is the exact ``RagContext.to_request_mapping()`` mapping,
    serialized with the project canonical JSON rules (sorted keys, compact
    separators, ASCII-safe), introduced by the ``RETRIEVED_CONTEXT`` marker
    and placed immediately before ``OUTPUT_REQUIREMENTS`` so the output
    contract remains the final instruction.
    """

    if v12_payload.count(_V12_MARKER) != 1:
        raise PayloadError(
            f"assembled payload must contain exactly one OUTPUT_REQUIREMENTS "
            f"marker; count={v12_payload.count(_V12_MARKER)}"
        )
    prefix, suffix = v12_payload.split(_V12_MARKER, 1)
    block = RETRIEVED_CONTEXT_MARKER + canonical_json(rag_mapping) + "\n"
    return prefix + block + _V12_MARKER + suffix


def canonical_rag_block(rag_mapping: Dict[str, Any]) -> str:
    """The canonical JSON text of the retrieved-context block (the exact
    serialization embedded in the assembled prompt)."""

    return canonical_json(rag_mapping)


def atomic_write_text(path: Any, text: str) -> None:
    """LF-explicit atomic text write.

    On Windows, ``Path.write_text`` translates ``\\n`` to ``\\r\\n``; the
    frozen protocol artifacts are LF (Colab).  All S4 text evidence (raw
    outputs, meta, retrieval records, patch files, identities) is written
    LF-explicit so on-disk bytes equal the canonical text.
    """

    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_prompt_budget(
    *,
    base_prompt: str,
    retrieved_context_bytes: int,
    assembled_prompt: str,
    assembled_prompt_tokens: Optional[int],
    base_prompt_tokens_manifest: Optional[int],
    rag_context_truncated: bool,
) -> Dict[str, Any]:
    """Record the independent prompt/context budget breakdown (Amendment 2).

    ``assembled_prompt_tokens`` is the exact tokenizer count when available
    (``--validate-only`` computes it with the frozen tokenizer); otherwise
    it stays ``None`` and is recorded as NOT_RECORDED.
    """

    base_prompt_bytes = len(base_prompt.encode("utf-8"))
    assembled_prompt_bytes = len(assembled_prompt.encode("utf-8"))
    max_prompt_tokens_ok = (
        assembled_prompt_tokens is None
        or assembled_prompt_tokens <= MAX_PROMPT_TOKENS
    )
    if assembled_prompt_tokens is not None and not max_prompt_tokens_ok:
        raise PayloadError(
            "frozen constraints cannot coexist: assembled prompt "
            f"{assembled_prompt_tokens} tokens exceeds max_prompt_tokens "
            f"{MAX_PROMPT_TOKENS}"
        )
    return {
        "base_prompt_bytes": base_prompt_bytes,
        "retrieved_context_bytes": retrieved_context_bytes,
        "assembled_prompt_bytes": assembled_prompt_bytes,
        "assembled_prompt_tokens": assembled_prompt_tokens,
        "assembled_prompt_tokens_NOT_RECORDED": assembled_prompt_tokens is None,
        "base_prompt_tokens_manifest": base_prompt_tokens_manifest,
        "protocol_max_prompt_tokens": MAX_PROMPT_TOKENS,
        "protocol_max_new_tokens": MAX_NEW_TOKENS,
        "rag_context_truncated": rag_context_truncated,
        "max_prompt_tokens_ok": bool(max_prompt_tokens_ok),
        "public_request_byte_budget": 20_000,
        "public_request_byte_budget_scope": (
            "LiveModelAdapter agentic public-request mapping only "
            "(agentic_debugger/evaluation/live.py); NOT the one-shot "
            "generation prompt limit"
        ),
    }
