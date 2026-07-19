import io
import json
import tempfile
from pathlib import Path

import pytest

from agentic_debugger import SchemaValidationError
from agentic_debugger.events.logger import JsonlEventLogger
from agentic_debugger.events.schema import (
    RunEvent, EventType, Metadata, validate_json_compatible,
)


def _make_event(sequence: int, run_id="run-001", task_id="task-001", **kw):
    base = {
        "schema_version": "1.0",
        "event_id": f"evt-{sequence:04d}",
        "run_id": run_id,
        "task_id": task_id,
        "sequence": sequence,
        "timestamp": "2026-07-18T12:00:00Z",
        "event_type": "action",
        "name": "test_action",
        "state": "Reproduce",
        "payload": {"key": "value"},
        "metadata": {"duration_ms": None, "tool_version": None, "model": None, "tokens": None, "cost": None},
    }
    base.update(kw)
    return RunEvent.from_mapping(base)


class TestJsonlEventLogger:
    def test_multiple_events_one_json_per_line(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.append(_make_event(1))
        logger.append(_make_event(2))
        logger.flush()
        stream.seek(0)
        lines = stream.read().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert isinstance(obj, dict)

    def test_event_order_preserved(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0, name="first"))
        logger.append(_make_event(1, name="second"))
        logger.append(_make_event(2, name="third"))
        logger.flush()
        stream.seek(0)
        lines = stream.read().strip().split("\n")
        names = [json.loads(l)["name"] for l in lines]
        assert names == ["first", "second", "third"]

    def test_sequences_must_be_contiguous(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        with pytest.raises(SchemaValidationError, match="non-contiguous"):
            logger.append(_make_event(2))

    def test_duplicate_sequence_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.append(_make_event(1))
        with pytest.raises(SchemaValidationError, match="duplicate|out-of-order"):
            logger.append(_make_event(1))

    def test_out_of_order_sequence_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.append(_make_event(1))
        with pytest.raises(SchemaValidationError, match="duplicate|out-of-order"):
            logger.append(_make_event(0))

    def test_first_event_not_zero_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        with pytest.raises(SchemaValidationError, match="first event.*sequence 0"):
            logger.append(_make_event(1))

    def test_negative_sequence_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        with pytest.raises(SchemaValidationError, match="non-negative"):
            logger.append(_make_event(-1))

    def test_mixed_run_id_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        with pytest.raises(SchemaValidationError, match="run_id"):
            logger.append(_make_event(1, run_id="run-002"))

    def test_mixed_task_id_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        with pytest.raises(SchemaValidationError, match="task_id"):
            logger.append(_make_event(1, task_id="task-002"))

    def test_utf8_output_parseable(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0, name="résumé"))
        logger.flush()
        stream.seek(0)
        raw = stream.read()
        obj = json.loads(raw.strip())
        assert obj["name"] == "résumé"

    def test_flush_behavior(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.flush()
        stream.seek(0)
        content_before = stream.read()
        assert content_before != ""

    def test_close_behavior(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.close()
        assert logger.closed

    def test_append_after_close_rejected(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.close()
        with pytest.raises(SchemaValidationError, match="closed"):
            logger.append(_make_event(1))

    def test_file_path_logging(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", encoding="utf-8", delete=False
        ) as f:
            tmp_path = f.name
        try:
            logger = JsonlEventLogger("run-001", "task-001", path=tmp_path)
            logger.append(_make_event(0))
            logger.append(_make_event(1))
            logger.close()

            with open(tmp_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 2
            for line in lines:
                obj = json.loads(line)
                assert isinstance(obj, dict)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_no_leftover_artifacts(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", encoding="utf-8", delete=False
        ) as f:
            tmp_path = f.name
        logger = JsonlEventLogger("run-001", "task-001", path=tmp_path)
        logger.append(_make_event(0))
        logger.close()
        assert Path(tmp_path).exists()
        Path(tmp_path).unlink()
        assert not Path(tmp_path).exists()

    def test_stream_append_after_close_no_write(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.close()
        before = stream.getvalue()
        with pytest.raises(SchemaValidationError):
            logger.append(_make_event(1))
        stream.seek(0)
        after = stream.read()
        assert after == before

    def test_zero_sequence_accepted_as_first(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.flush()
        stream.seek(0)
        obj = json.loads(stream.read().strip())
        assert obj["sequence"] == 0

    def test_deterministic_output(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        logger.append(_make_event(0))
        logger.flush()
        output1 = stream.getvalue()
        stream2 = io.StringIO()
        logger2 = JsonlEventLogger("run-001", "task-001", stream=stream2)
        logger2.append(_make_event(0))
        logger2.flush()
        output2 = stream2.getvalue()
        assert output1 == output2

    def test_path_and_stream_both_provided_rejected(self):
        with pytest.raises(SchemaValidationError, match="not both"):
            JsonlEventLogger("run-001", "task-001", path="/tmp/x", stream=io.StringIO())

    def test_neither_path_nor_stream_rejected(self):
        with pytest.raises(SchemaValidationError, match="either path or stream"):
            JsonlEventLogger("run-001", "task-001")

    def test_empty_run_id_rejected(self):
        with pytest.raises(SchemaValidationError, match="non-empty"):
            JsonlEventLogger("", "task-001", stream=io.StringIO())

    def test_empty_task_id_rejected(self):
        with pytest.raises(SchemaValidationError, match="non-empty"):
            JsonlEventLogger("run-001", "", stream=io.StringIO())

    # --- Issue 3: logger NaN/Infinity rejection tests ---

    def test_logger_allow_nan_false_rejects_serialization(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        meta = Metadata(duration_ms=None, tool_version=None, model=None,
                        tokens=None, cost=None)
        ev = RunEvent(
            schema_version="1.0",
            event_id="evt-test",
            run_id="run-001",
            task_id="task-001",
            sequence=0,
            timestamp="2026-07-18T12:00:00Z",
            event_type=EventType.ACTION,
            name="test",
            state="Reproduce",
            payload={"x": float("nan")},
            metadata=meta,
        )
        with pytest.raises(SchemaValidationError, match="Non-finite"):
            logger.append(ev)

    def test_finite_float_round_trip_via_logger(self):
        stream = io.StringIO()
        logger = JsonlEventLogger("run-001", "task-001", stream=stream)
        ev = _make_event(0, payload={"pi": 3.14159})
        logger.append(ev)
        logger.flush()
        stream.seek(0)
        obj = json.loads(stream.read().strip())
        assert obj["payload"]["pi"] == 3.14159
