from deadline_pipeline import request_deadline


def test_seconds_are_converted_before_retry_expansion() -> None:
    assert request_deadline(2, " SECONDS ", 2, 250) == 6250


def test_milliseconds_keep_their_unit_during_retry_expansion() -> None:
    assert request_deadline(400, "milliseconds", 1, 50) == 850


def test_zero_delay_keeps_only_the_grace_period() -> None:
    assert request_deadline(0, "seconds", 3, 75) == 75
