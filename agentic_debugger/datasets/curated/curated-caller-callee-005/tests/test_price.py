from price import format_price


def test_dollar_representation_is_converted_at_boundary() -> None:
    assert format_price(12, "dollars") == "$12.00"


def test_zero_dollars_formats_as_zero() -> None:
    assert format_price(0, "dollars") == "$0.00"


def test_cents_representation_formats_standard_value() -> None:
    assert format_price(125, "cents") == "$1.25"
