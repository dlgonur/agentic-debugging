from recent_window import recent_window


def test_full_length_window_includes_every_value() -> None:
    values = [10, 20, 30, 40]
    assert recent_window(values, len(values)) == values


def test_smaller_window_returns_recent_values() -> None:
    assert recent_window([10, 20, 30, 40], 2) == [30, 40]


def test_zero_window_is_empty() -> None:
    assert recent_window([10, 20, 30, 40], 0) == []
