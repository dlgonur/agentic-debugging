from access_branch import choose_access


def test_employee_with_pass_gets_priority_access() -> None:
    assert choose_access(True, True) == "priority"


def test_employee_without_pass_gets_employee_access() -> None:
    assert choose_access(True, False) == "employee"


def test_pass_holder_without_employee_flag_gets_guest_access() -> None:
    assert choose_access(False, True) == "guest-pass"


def test_without_flags_is_denied() -> None:
    assert choose_access(False, False) == "denied"
