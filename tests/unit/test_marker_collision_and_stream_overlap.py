"""V2-02 repair 12 — marker collision and stream overlap invariance tests.

Reproduction and acceptance tests for:
- Finding A: replacement markers themselves cannot contain raw secret values
  (self-collision, cross-binding collision, marker vocabulary collision).
- Finding B: streaming redaction is chunk-boundary invariant for overlapping
  secret values and cannot manufacture a raw secret fragment.

No provider network, no real credentials, no frozen experiment.
"""

from __future__ import annotations

import copy
import os
import pickle
import sys
from pathlib import Path

import pytest

from agentic_debugger.application.execution_environment import (
    PDB_BOUNDED_TEXT_TRUNCATION_MARKER,
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ProjectSecretRedactor,
)
from agentic_debugger.application.executor import ProductExecutor
from agentic_debugger.application.session_runtime import (
    ProjectEnvDeclaration,
    ProjectRuntimeEnvironmentSpec,
    build_local_project_launch,
)
from agentic_debugger.runtime.workspace import TaskWorkspace


# ===========================================================================
# Finding A: Marker self/cross collision safety
# ===========================================================================


def test_finding_a1_marker_self_collision_single_char():
    """A1: name='A', value='A'. Redacting 'A' must NOT contain raw 'A'."""
    redactor = ProjectSecretRedactor((("A", "A"),))
    result = redactor.redact("A")
    assert "A" not in result, f"Emitted marker leaked raw secret: {result!r}"


def test_finding_a2_marker_vocabulary_collision():
    """A2: name='TOKEN_NAME', value='PROJECT_SECRET'.
    The output must NOT contain 'PROJECT_SECRET'."""
    redactor = ProjectSecretRedactor((("TOKEN_NAME", "PROJECT_SECRET"),))
    result = redactor.redact("PROJECT_SECRET")
    assert "PROJECT_SECRET" not in result, (
        f"Emitted marker leaked raw secret: {result!r}"
    )


def test_finding_a3_cross_binding_collision_value_equals_other_name():
    """A3: one secret VALUE equals another secret NAME or marker vocabulary."""
    redactor = ProjectSecretRedactor((
        ("SECRET_FOO", "bar_val"),
        ("OTHER_NAME", "SECRET_FOO"),
    ))
    # Redacting 'bar_val' (whose name is SECRET_FOO, which is OTHER_NAME's value)
    out_bar = redactor.redact("prefix bar_val suffix")
    assert "bar_val" not in out_bar
    assert "SECRET_FOO" not in out_bar, (
        f"Emitted marker for bar_val contained OTHER_NAME's secret value: {out_bar!r}"
    )

    out_foo = redactor.redact("prefix SECRET_FOO suffix")
    assert "SECRET_FOO" not in out_foo
    assert "bar_val" not in out_foo


def test_finding_a3_cross_binding_collision_substring_of_marker_vocab():
    """A3: secret value is a substring of default marker vocabulary."""
    redactor = ProjectSecretRedactor((
        ("S1", "PROJECT"),
        ("S2", "SECRET"),
        ("S3", "PROJECT_SECRET"),
    ))
    all_secrets = ("PROJECT", "SECRET", "PROJECT_SECRET")
    for secret in all_secrets:
        output = redactor.redact(f"echo {secret}")
        for s in all_secrets:
            assert s not in output, (
                f"Secret {s!r} leaked in output {output!r} when redacting {secret!r}"
            )


def test_finding_a4_multiple_secrets_disjoint_markers():
    """A4: For every output produced by redacting each value,
    all(secret_val not in output for every non-empty session secret)."""
    bindings = (
        ("K1", "ALPHA"),
        ("K2", "BETA"),
        ("K3", "GAMMA"),
        ("K4", "DELTA"),
        ("K5", "ALPHA_EXT"),
    )
    redactor = ProjectSecretRedactor(bindings)
    all_vals = tuple(val for _, val in bindings)

    for name, val in bindings:
        out = redactor.redact(f"log {val} end")
        for s in all_vals:
            assert s not in out, f"Secret {s!r} found in output: {out!r}"


def test_finding_a5_normal_synthetic_secrets_deterministic_format():
    """A5: Normal non-colliding secrets retain deterministic format without raw value."""
    name = "SYNTHETIC_API_KEY"
    val = "sk-proj-xyz123abc456"
    redactor = ProjectSecretRedactor(((name, val),))
    out = redactor.redact(f"key={val}")
    assert val not in out
    assert f"<PROJECT_SECRET:{name}>" in out


def test_finding_a_pathological_all_characters_colliding():
    """Fail-safe: when readable candidates collide, fallback removes value safely."""
    # Secrets covering all common template characters
    redactor = ProjectSecretRedactor((
        ("P", "PROJECT"),
        ("S", "SECRET"),
        ("R", "REDACTED"),
        ("BRACKET1", "<"),
        ("BRACKET2", ">"),
        ("COLON", ":"),
    ))
    all_vals = ("PROJECT", "SECRET", "REDACTED", "<", ">", ":")
    out = redactor.redact("PROJECT SECRET REDACTED < > :")
    for s in all_vals:
        assert s not in out, f"Secret {s!r} leaked in pathological case: {out!r}"


def test_finding_a_pdb_bounded_text_safe_marker():
    """PDB marked bounded diagnostic safe against marker vocabulary collision."""
    redactor = ProjectSecretRedactor((("TOKEN_NAME", "PROJECT_SECRET"),))
    # Cut prefix of PROJECT_SECRET at boundary before marker
    raw_diagnostic = "Traceback at PROJECT_SEC" + PDB_BOUNDED_TEXT_TRUNCATION_MARKER
    redacted = redactor.redact_bounded_text(raw_diagnostic)
    assert "PROJECT_SECRET" not in redacted, (
        f"Raw secret appeared in bounded diagnostic: {redacted!r}"
    )


def test_finding_a_pdb_truncated_string_preview_safe_marker():
    """PDB marked string preview safe against marker vocabulary collision."""
    redactor = ProjectSecretRedactor((("TOKEN_NAME", "PROJECT_SECRET"),))
    preview = "PROJECT_SEC"
    redacted = redactor.redact_truncated_string_preview(preview)
    assert "PROJECT_SECRET" not in redacted, (
        f"Raw secret appeared in truncated preview: {redacted!r}"
    )


def test_finding_a_structure_recursion_safe_marker():
    """redact_structure produces safe markers for colliding secrets."""
    redactor = ProjectSecretRedactor((("A", "A"),))
    struct = {
        "kind": "str",
        "truncated": True,
        "value": "A",
        "items": ["A", {"nested": "A"}],
    }
    redacted = redactor.redact_structure(struct)
    assert "A" not in redacted["value"]
    assert "A" not in redacted["items"][0]
    assert "A" not in redacted["items"][1]["nested"]


def test_finding_a_repr_and_fail_closed():
    """repr and serialization remain safe and fail-closed."""
    redactor = ProjectSecretRedactor((("A", "A"),))
    r = repr(redactor)
    assert "A" not in r
    assert "redactable_secrets=1" in r
    with pytest.raises(TypeError, match="must never be serialized"):
        pickle.dumps(redactor)
    with pytest.raises(TypeError):
        copy.deepcopy(redactor)


# ===========================================================================
# Finding B: Streaming overlap and chunk-boundary invariance
# ===========================================================================


def test_finding_b1_firstmate_exact_reproduction_single_chars():
    """B1: Exact FirstMate reproduction.
    LONG='AAAA', SHORT='AAA', input='qAAAA', feed one character at a time.
    Output must equal canonical full-input redaction; no trailing raw 'A'.
    """
    redactor = ProjectSecretRedactor((
        ("LONG", "AAAA"),
        ("SHORT", "AAA"),
    ))
    text = "qAAAA"
    canonical = redactor.redact(text)

    sanitizer = redactor.stream_sanitizer_factory()()
    emitted = []
    for ch in text:
        emitted.append(sanitizer.feed(ch))
    emitted.append(sanitizer.flush())
    streamed = "".join(emitted)

    assert streamed == canonical, (
        f"Streamed {streamed!r} did not match canonical {canonical!r}"
    )
    assert not streamed.endswith("A"), (
        f"Streamed output manufactured trailing raw fragment: {streamed!r}"
    )


@pytest.mark.parametrize(
    "segmentation",
    [
        [5],
        [1, 4],
        [2, 3],
        [3, 2],
        [4, 1],
        [1, 1, 1, 1, 1],
    ],
)
def test_finding_b2_segmentation_invariance(segmentation):
    """B2: Repeat 'qAAAA' with several segmentations; all must be identical."""
    redactor = ProjectSecretRedactor((
        ("LONG", "AAAA"),
        ("SHORT", "AAA"),
    ))
    text = "qAAAA"
    canonical = redactor.redact(text)

    chunks = []
    idx = 0
    for length in segmentation:
        chunks.append(text[idx : idx + length])
        idx += length

    sanitizer = redactor.stream_sanitizer_factory()()
    out = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
    assert out == canonical, (
        f"Segmentation {segmentation} produced {out!r} != canonical {canonical!r}"
    )


def test_finding_b3_longer_mixed_input_various_chunk_sizes():
    """B3: Longer mixed input containing repeated overlapping LONG/SHORT values
    and ordinary text across various chunk sizes."""
    redactor = ProjectSecretRedactor((
        ("LONG", "AAAA"),
        ("SHORT", "AAA"),
        ("OTHER", "BBBB"),
    ))
    text = (
        "prefix_text_qAAAA_middle_AAA_AAAAA_BBBB_end_AAAA_more_text_"
        * 10
    )
    canonical = redactor.redact(text)

    for chunk_size in [1, 2, 3, 4, 5, 7, 13, 32, 64, 128]:
        chunks = [
            text[i : i + chunk_size]
            for i in range(0, len(text), chunk_size)
        ]
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == canonical, (
            f"Chunk size {chunk_size} mismatch:\nStreamed: {streamed[:100]!r}...\nCanonical: {canonical[:100]!r}..."
        )


def test_finding_b4_equal_values_different_names_tiebreak():
    """B4: Equal values under different names preserve canonical tiebreak
    independently of chunking."""
    redactor = ProjectSecretRedactor((
        ("SEC_Z", "shared_value"),
        ("SEC_A", "shared_value"),
    ))
    text = "pre_shared_value_post"
    canonical = redactor.redact(text)

    for chunk_size in [1, 2, 3, 5, 10, len(text)]:
        chunks = [
            text[i : i + chunk_size]
            for i in range(0, len(text), chunk_size)
        ]
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == canonical, (
            f"Tiebreak changed for chunk_size {chunk_size}: {streamed!r} != {canonical!r}"
        )


def test_finding_b5_secret_prefix_at_end_of_stream():
    """B5: Secret prefix at end-of-stream is flushed correctly."""
    redactor = ProjectSecretRedactor((("TOKEN", "LONG_SECRET_VAL"),))
    text = "some text with LONG_SEC"
    canonical = redactor.redact(text)
    assert canonical == text  # incomplete secret was not transformed

    for chunk_size in [1, 2, 5, len(text)]:
        chunks = [
            text[i : i + chunk_size]
            for i in range(0, len(text), chunk_size)
        ]
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == canonical, (
            f"Prefix flush mismatch for chunk_size {chunk_size}: {streamed!r} != {canonical!r}"
        )


def test_finding_b6_ordinary_non_secret_text_unchanged():
    """B6: Ordinary non-secret text remains byte-for-byte unchanged."""
    redactor = ProjectSecretRedactor((("KEY", "SECRET_KEY_XYZ"),))
    text = "The quick brown fox jumps over the lazy dog. 1234567890!@#$%^&*()"
    for chunk_size in [1, 3, 7, len(text)]:
        chunks = [
            text[i : i + chunk_size]
            for i in range(0, len(text), chunk_size)
        ]
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == text


def test_finding_b7_empty_secret_semantics():
    """B7: Empty-secret semantics remain inert."""
    redactor = ProjectSecretRedactor((
        ("EMPTY", ""),
        ("REAL", "REAL_SECRET"),
    ))
    text = "echo REAL_SECRET and text"
    canonical = redactor.redact(text)

    for chunk_size in [1, 2, 5, len(text)]:
        chunks = [
            text[i : i + chunk_size]
            for i in range(0, len(text), chunk_size)
        ]
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == canonical


def test_finding_b8_determinism_invariant():
    """Invariant: for any fixed binding set and complete input, several different
    deterministic chunk partitions all produce the exact same sanitized output."""
    bindings = (
        ("L1", "MATCH_LONGEST_SECRET"),
        ("L2", "MATCH_LONG"),
        ("L3", "MATCH"),
    )
    redactor = ProjectSecretRedactor(bindings)
    text = "START_MATCH_LONGEST_SECRET_MID_MATCH_LONG_END_MATCH_FIN"
    canonical = redactor.redact(text)

    partitions = [
        [len(text)],
        [1] * len(text),
        [2] * (len(text) // 2) + ([len(text) % 2] if len(text) % 2 else []),
        [3] * (len(text) // 3) + ([len(text) % 3] if len(text) % 3 else []),
        [5, 10, 15, len(text) - 30],
        [7, 14, 21, len(text) - 42],
    ]
    for p in partitions:
        chunks = []
        cur = 0
        for sz in p:
            if sz > 0:
                chunks.append(text[cur : cur + sz])
                cur += sz
        sanitizer = redactor.stream_sanitizer_factory()()
        streamed = "".join(sanitizer.feed(c) for c in chunks) + sanitizer.flush()
        assert streamed == canonical, f"Partition {p} differed from canonical"


def test_product_executor_overlapping_secrets_regression(tmp_path, monkeypatch):
    """Real ProductExecutor regression using overlapping secret bindings.
    Proves no chunk-produced raw fragment reaches CommandResult."""
    long_val = "OVERLAPPING_LONG_SECRET"
    short_val = "OVERLAPPING_LONG"

    monkeypatch.setenv("SEC_LONG", long_val)
    monkeypatch.setenv("SEC_SHORT", short_val)

    spec = ProjectRuntimeEnvironmentSpec(
        secrets=(
            ProjectEnvDeclaration("SEC_LONG"),
            ProjectEnvDeclaration("SEC_SHORT"),
        )
    )
    launch = build_local_project_launch(
        session_id="sess-v202-overlap",
        task_id="local-project-debug",
        policy="pdb-on-uncertainty",
        provider_id=None,
        model_id=None,
        profile_id="dummy-profile",
        launch_snapshot=dict(os.environ),
        project_spec=spec,
    )
    executor = ProductExecutor(
        execution_environment=launch.execution_environment,
        capabilities=launch.capabilities,
    )
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    workspace = TaskWorkspace(str(ws_dir))

    # Subprocess deliberately splits child stream across read chunks:
    # First chunk ends right after the SHORT secret, before the rest of LONG.
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys, time; "
            "sys.stdout.write('HEAD_' + os.environ['SEC_SHORT']); sys.stdout.flush(); "
            "time.sleep(0.05); "
            "sys.stdout.write(os.environ['SEC_LONG'][len(os.environ['SEC_SHORT']):] + '_TAIL\\n'); sys.stdout.flush()"
        ),
    ]
    res = executor.run_project_command(cmd, workspace, 10.0)

    assert res.exit_code == 0
    assert long_val not in res.stdout, "Raw long secret leaked"
    assert short_val not in res.stdout, "Raw short secret fragment leaked"
    redactor = launch.execution_environment.project_secret_redactor()
    assert res.stdout == redactor.redact("HEAD_" + long_val + f"_TAIL{os.linesep}")
