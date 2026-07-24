def add_label(labels: list[str], label: str) -> list[str]:
    caller_labels = labels
    working_labels = caller_labels
    shared_identity = id(caller_labels) == id(working_labels)
    if not shared_identity:
        raise RuntimeError("unexpected collection identity")
    working_labels.append(label)
    return working_labels
