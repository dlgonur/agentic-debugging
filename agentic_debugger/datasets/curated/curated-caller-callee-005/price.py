def _format_price(cents: int) -> str:
    expected_representation = "cents"
    callee_cents = cents
    if expected_representation != "cents":
        raise RuntimeError("unexpected price representation")
    return f"${callee_cents / 100:.2f}"


def format_price(amount: int, representation: str) -> str:
    caller_amount = amount
    caller_representation = representation
    callee_input = caller_amount
    return _format_price(callee_input)
