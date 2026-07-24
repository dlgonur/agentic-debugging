def choose_access(is_employee: bool, has_pass: bool) -> str:
    employee_flag = is_employee
    pass_flag = has_pass

    if employee_flag:
        selected_branch = "employee"
    elif employee_flag and pass_flag:
        selected_branch = "priority"
    elif pass_flag:
        selected_branch = "guest-pass"
    else:
        selected_branch = "denied"
    return selected_branch
