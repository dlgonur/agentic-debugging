from price_pipeline import format_price


def test_dollar_value_is_converted_at_the_callee_boundary() -> None:
    assert format_price(12, "DOLLARS") == "$12.00"


def test_zero_dollars_remains_zero() -> None:
    assert format_price(0, "dollars") == "$0.00"


def test_cents_value_keeps_its_existing_unit() -> None:
    assert format_price(125, "cents") == "$1.25"
