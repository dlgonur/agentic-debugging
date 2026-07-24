from labels import add_label


def test_original_labels_remain_unchanged() -> None:
    original = ["base"]
    add_label(original, "new")
    assert original == ["base"]


def test_returned_content_contains_new_label() -> None:
    assert add_label(["base"], "new") == ["base", "new"]


def test_repeated_calls_with_separate_inputs_are_independent() -> None:
    first = add_label(["one"], "two")
    second = add_label(["three"], "four")
    assert first == ["one", "two"]
    assert second == ["three", "four"]
