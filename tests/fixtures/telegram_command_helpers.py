from __future__ import annotations

from codex_autorunner.integrations.telegram.adapter import TelegramMessageEntity


async def noop_handler(*_args, **_kwargs) -> None:
    return None


def bot_command_entity(token: str, *, offset: int = 0) -> TelegramMessageEntity:
    return TelegramMessageEntity(type="bot_command", offset=offset, length=len(token))
