from __future__ import annotations

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCommand,
    parse_command,
)
from codex_autorunner.integrations.telegram.commands_registry import (
    build_command_payloads,
)
from tests.fixtures.telegram_command_helpers import (
    README_REVISIT_GUIDANCE_MODULE_THRESHOLD,
    bot_command_entity,
    make_command_spec,
)

# Helper usage: this file owns cross-cutting Telegram command invariants, so use
# shared helpers for setup and keep assertions focused on behavior contracts.


def test_contract_runtime_entity_and_fallback_parity_for_mention_validation() -> None:
    token = "/resume@x"
    entities = [bot_command_entity(token)]
    assert parse_command(f"{token} 3", entities=entities) is None
    assert parse_command(f"{token} 3") is None


def test_contract_runtime_parsing_normalizes_bot_username() -> None:
    token = "/resume@CodexBot"
    entities = [bot_command_entity(token)]
    parsed = parse_command(f"{token} 3", entities=entities, bot_username=" @codexbot ")
    assert parsed == TelegramCommand(name="resume", args="3", raw="/resume@CodexBot 3")


def test_contract_registration_normalizes_name_and_runtime_rejects_uppercase() -> None:
    specs = {"Review": make_command_spec("Review", "Review command")}
    commands, invalid = build_command_payloads(specs)

    assert invalid == []
    assert commands == [{"command": "review", "description": "Review command"}]
    assert parse_command("/Review") is None


def test_contract_registration_rejects_invalid_names() -> None:
    specs = {
        "invalid-hyphen": make_command_spec("foo-bar", "bad"),
        "invalid-too-long": make_command_spec("a" * 33, "bad"),
    }
    commands, invalid = build_command_payloads(specs)
    assert commands == []
    assert invalid == ["foo-bar", "a" * 33]


def test_contract_readme_revisit_threshold_policy_constant() -> None:
    assert README_REVISIT_GUIDANCE_MODULE_THRESHOLD == 6
