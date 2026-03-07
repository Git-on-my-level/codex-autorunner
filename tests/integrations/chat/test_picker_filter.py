from codex_autorunner.integrations.chat.picker_filter import (
    filter_picker_items,
    find_exact_picker_item,
)


def test_find_exact_picker_item_matches_value_and_alias() -> None:
    items = [("openai/gpt-5", "GPT-5"), ("zai/glm-5", "GLM 5")]
    aliases = {"zai/glm-5": ("glm",)}

    assert find_exact_picker_item(items, "openai/gpt-5") == ("openai/gpt-5", "GPT-5")
    assert find_exact_picker_item(items, "glm", aliases=aliases) == (
        "zai/glm-5",
        "GLM 5",
    )


def test_filter_picker_items_orders_prefix_before_substring() -> None:
    items = [
        ("openai/gpt-5", "GPT-5"),
        ("zai/glm-5", "GLM 5"),
        ("openai/gpt-4.1", "Legacy model with glm compatibility"),
    ]

    assert filter_picker_items(items, "glm", limit=5) == [
        ("zai/glm-5", "GLM 5"),
        ("openai/gpt-4.1", "Legacy model with glm compatibility"),
    ]


def test_filter_picker_items_supports_multi_token_query() -> None:
    items = [
        ("run-20260101-a", "alpha"),
        ("run-20260102-b", "beta"),
        ("flow-20260101", "gamma"),
    ]

    assert filter_picker_items(items, "run 202601", limit=5) == [
        ("run-20260101-a", "alpha"),
        ("run-20260102-b", "beta"),
    ]
