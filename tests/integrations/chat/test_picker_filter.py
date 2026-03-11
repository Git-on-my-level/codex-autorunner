from codex_autorunner.integrations.chat.picker_filter import (
    filter_picker_items,
    find_exact_picker_item,
    resolve_picker_query,
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


def test_resolve_picker_query_uses_aliases_for_filter_without_forcing_exact_match() -> (
    None
):
    items = [("run-a", "run-a"), ("run-b", "run-b")]
    aliases = {"run-a": ("paused",), "run-b": ("paused",)}

    resolution = resolve_picker_query(items, "paused", limit=5, aliases=aliases)

    assert resolution.selected_value is None
    assert resolution.filtered_items == [("run-a", "run-a"), ("run-b", "run-b")]


def test_resolve_picker_query_supports_exact_aliases_separately() -> None:
    items = [("repo@token", "repo-a")]
    resolution = resolve_picker_query(
        items,
        "/tmp/repo-a",
        limit=5,
        exact_aliases={"repo@token": ("/tmp/repo-a",)},
        aliases={"repo@token": ("/tmp/repo-a", "repo-a", "repo")},
    )

    assert resolution.selected_value == "repo@token"
    assert resolution.filtered_items == []
