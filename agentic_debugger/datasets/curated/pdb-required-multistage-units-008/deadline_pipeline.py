def _to_milliseconds(value: int, factor: int) -> int:
    return value * factor

def _expand_retry_window(delay_ms: int, retries: int) -> int:
    return delay_ms * (retries + 1)

def request_deadline(value: int, unit: str, retries: int, grace_ms: int) -> int:
    normalized_unit = unit.strip().lower()
    conversion_factor = {"seconds": 1000, "milliseconds": 1}[normalized_unit]
    base_delay_ms = _to_milliseconds(value, conversion_factor)
    retry_count = max(retries, 0)
    retry_window_ms = _expand_retry_window(value, retry_count)
    return retry_window_ms + grace_ms
