from __future__ import annotations

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCommand,
    TelegramMessageEntity,
    parse_command,
)
from codex_autorunner.integrations.telegram.commands_registry import (
    build_command_payloads,
)
from codex_autorunner.integrations.telegram.handlers.commands import CommandSpec


async def _noop_handler(*_args, **_kwargs) -> None:
    return None


def test_contract_runtime_entity_and_fallback_parity_for_mention_validation() -> None:
    token = "/resume@x"
    entities = [TelegramMessageEntity(type="bot_command", offset=0, length=len(token))]
    assert parse_command(f"{token} 3", entities=entities) is None
    assert parse_command(f"{token} 3") is None


def test_contract_runtime_parsing_normalizes_bot_username() -> None:
    token = "/resume@CodexBot"
    entities = [TelegramMessageEntity(type="bot_command", offset=0, length=len(token))]
    parsed = parse_command(f"{token} 3", entities=entities, bot_username=" @codexbot ")
    assert parsed == TelegramCommand(name="resume", args="3", raw="/resume@CodexBot 3")


def test_contract_registration_normalizes_name_and_runtime_rejects_uppercase() -> None:
    specs = {"Review": CommandSpec("Review", "Review command", _noop_handler)}
    commands, invalid = build_command_payloads(specs)

    assert invalid == []
    assert commands == [{"command": "review", "description": "Review command"}]
    assert parse_command("/Review") is None


def test_contract_registration_rejects_invalid_names() -> None:
    specs = {
        "invalid-hyphen": CommandSpec("foo-bar", "bad", _noop_handler),
        "invalid-too-long": CommandSpec("a" * 33, "bad", _noop_handler),
    }
    commands, invalid = build_command_payloads(specs)
    assert commands == []
    assert invalid == ["foo-bar", "a" * 33]
