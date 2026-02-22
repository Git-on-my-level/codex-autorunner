from __future__ import annotations

import pytest

from codex_autorunner.integrations.chat.commands import (
    MAX_COMMAND_MENTION_LENGTH,
    MAX_COMMAND_NAME_LENGTH,
    MIN_COMMAND_MENTION_LENGTH,
    MIN_COMMAND_NAME_LENGTH,
    parse_chat_command,
)


@pytest.mark.parametrize(
    ("text", "expected_name", "expected_args"),
    [
        ("/status", "status", ""),
        ("/status   now", "status", "now"),
        ("/status\tfast", "status", "fast"),
        ("/status\nfast", "status", "fast"),
        ("  /status now  ", "status", "now"),
    ],
)
def test_parse_chat_command_valid_inputs(
    text: str, expected_name: str, expected_args: str
) -> None:
    parsed = parse_chat_command(text)
    assert parsed is not None
    assert parsed.name == expected_name
    assert parsed.args == expected_args


@pytest.mark.parametrize(
    ("text", "bot_username", "should_parse"),
    [
        ("/status@mybot now", "mybot", True),
        ("/status@MYBOT now", "mybot", True),
        ("/status@mybot now", "@mybot", True),
        ("/status@otherbot now", "mybot", False),
        ("/status@mybot now", None, True),
    ],
)
def test_parse_chat_command_mention_filtering(
    text: str, bot_username: str | None, should_parse: bool
) -> None:
    parsed = parse_chat_command(text, bot_username=bot_username)
    if should_parse:
        assert parsed is not None
        assert parsed.name == "status"
        assert parsed.args == "now"
        return
    assert parsed is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "status",
        "/",
        "/UPPER",
        "/bad-name",
        "/status@ab now",
        "/status@@bot now",
        "/status@bot! now",
    ],
)
def test_parse_chat_command_malformed_inputs_return_none(text: str) -> None:
    assert parse_chat_command(text) is None


def test_parse_chat_command_accepts_name_length_boundaries() -> None:
    min_name = "a" * MIN_COMMAND_NAME_LENGTH
    max_name = "a" * MAX_COMMAND_NAME_LENGTH

    parsed_min = parse_chat_command(f"/{min_name}")
    parsed_max = parse_chat_command(f"/{max_name}")

    assert parsed_min is not None
    assert parsed_min.name == min_name
    assert parsed_max is not None
    assert parsed_max.name == max_name


def test_parse_chat_command_rejects_name_above_max_length() -> None:
    over_max_name = "a" * (MAX_COMMAND_NAME_LENGTH + 1)
    assert parse_chat_command(f"/{over_max_name}") is None


def test_parse_chat_command_accepts_mention_length_boundaries() -> None:
    mention_min = "b" * MIN_COMMAND_MENTION_LENGTH
    mention_max = "b" * MAX_COMMAND_MENTION_LENGTH

    parsed_min = parse_chat_command(f"/status@{mention_min} now")
    parsed_max = parse_chat_command(f"/status@{mention_max} now")

    assert parsed_min is not None
    assert parsed_min.args == "now"
    assert parsed_max is not None
    assert parsed_max.args == "now"


def test_parse_chat_command_rejects_mention_outside_length_bounds() -> None:
    mention_too_short = "b" * (MIN_COMMAND_MENTION_LENGTH - 1)
    mention_too_long = "b" * (MAX_COMMAND_MENTION_LENGTH + 1)

    assert parse_chat_command(f"/status@{mention_too_short} now") is None
    assert parse_chat_command(f"/status@{mention_too_long} now") is None


@pytest.mark.parametrize(
    "text",
    [
        "/Status now",
        "/STATUS now",
        "!/status now",
        "./status now",
        "please /status now",
    ],
)
def test_parse_chat_command_rejects_current_non_goal_forms(text: str) -> None:
    assert parse_chat_command(text) is None
