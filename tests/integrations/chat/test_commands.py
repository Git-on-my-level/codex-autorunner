from __future__ import annotations

import pytest

from codex_autorunner.integrations.chat.commands import parse_chat_command


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
