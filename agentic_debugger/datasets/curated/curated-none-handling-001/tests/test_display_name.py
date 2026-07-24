from display_name import format_display_name


def test_missing_display_name_returns_fallback() -> None:
    assert format_display_name(None) == "Anonymous"


def test_regular_display_name_is_formatted() -> None:
    assert format_display_name("Ada Lovelace") == "Ada Lovelace"


def test_whitespace_is_normalized() -> None:
    assert format_display_name("  grace hopper ") == "Grace Hopper"
