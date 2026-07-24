def recent_window(values: list[int], size: int) -> list[int]:
    sequence_length = len(values)
    requested_size = size
    if requested_size <= 0 or sequence_length == 0:
        return []

    start_index = max(sequence_length - requested_size, 0)
    end_index = sequence_length
    calculated_indexes = list(
        range(start_index, end_index - (1 if requested_size == sequence_length else 0))
    )
    return [values[index] for index in calculated_indexes]
