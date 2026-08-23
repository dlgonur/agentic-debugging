def _render_cents(cents: int) -> str:
    expected_unit = "cents"
    callee_amount = cents
    if expected_unit != "cents":
        raise RuntimeError("unexpected price unit")
    return f"${callee_amount / 100:.2f}"


def format_price(amount: int, representation: str) -> str:
    caller_amount = amount
    caller_representation = representation.strip().lower()
    callee_input = caller_amount
    return _render_cents(callee_input)
