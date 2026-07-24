def format_display_name(name: str | None) -> str:
    normalized_name = name.strip()
    if not normalized_name:
        return "Anonymous"
    return normalized_name.title()
