from __future__ import annotations

from typing import Any, Awaitable, Callable

from codex_autorunner.integrations.telegram.adapter import (
    TelegramMessage,
    TelegramMessageEntity,
)
from codex_autorunner.integrations.telegram.handlers.commands import CommandSpec

CommandHandler = Callable[[TelegramMessage, str, Any], Awaitable[None]]

# Trigger-based README revisit note:
# Keep helper-usage guidance local to test modules until guidance expands to
# six or more Telegram command test modules. At that point, re-evaluate adding
# a shared README to improve discoverability.
README_REVISIT_GUIDANCE_MIN_MODULE_THRESHOLD = 6


async def noop_handler(_message: TelegramMessage, _args: str, _runtime: Any) -> None:
    return None


def bot_command_entity(token: str, *, offset: int = 0) -> TelegramMessageEntity:
    return TelegramMessageEntity(type="bot_command", offset=offset, length=len(token))


def make_command_spec(
    name: str,
    description: str,
    *,
    handler: CommandHandler = noop_handler,
    allow_during_turn: bool = False,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        description=description,
        handler=handler,
        allow_during_turn=allow_during_turn,
    )
