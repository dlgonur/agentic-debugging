from window_tail import tail_window


def test_full_window_keeps_the_boundary_value() -> None:
    assert tail_window([10, 20, 30, 40], 4) == [10, 20, 30, 40]


def test_smaller_window_returns_the_recent_values() -> None:
    assert tail_window([10, 20, 30, 40], 2) == [30, 40]
