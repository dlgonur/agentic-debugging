def tail_window(values: list[int], size: int) -> list[int]:
    item_count = len(values)
    requested_size = size
    if requested_size <= 0 or item_count == 0:
        return []

    start_index = max(item_count - requested_size, 0)
    end_index = item_count
    selected = values[start_index:end_index - (1 if requested_size == item_count else 0)]
    return selected
