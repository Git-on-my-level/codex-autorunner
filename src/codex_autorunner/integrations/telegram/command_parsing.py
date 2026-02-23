"""Shared command parsing helpers for Telegram adapters."""

from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

from ..chat.commands import parse_chat_command
from .api_schemas import TelegramMessageEntitySchema


def parse_command_payload(
    text: Optional[str],
    *,
    entities: Optional[Sequence[TelegramMessageEntitySchema | object]] = None,
    bot_username: Optional[str] = None,
) -> Optional[Tuple[str, str, str]]:
    """Return parsed command tuple ``(name, args, raw)`` or ``None``."""
    if not text:
        return None

    if entities:
        payload = _parse_command_from_entities(
            text, entities=entities, bot_username=bot_username
        )
        if payload is not None:
            return payload
        return None

    return _parse_command_from_text(text, bot_username=bot_username)


def _parse_command_from_entities(
    text: str,
    *,
    entities: Sequence[Any],
    bot_username: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    def _has_offset(entity: Any, expected: int) -> bool:
        value = getattr(entity, "offset", None)
        return isinstance(value, int) and value == expected

    def _has_length(entity: Any) -> Optional[int]:
        value = getattr(entity, "length", None)
        return value if isinstance(value, int) else None

    def _is_bot_command_entity(entity: Any) -> bool:
        return getattr(entity, "type", None) == "bot_command" and _has_offset(entity, 0)

    command_entity = next(
        (entity for entity in entities if _is_bot_command_entity(entity)),
        None,
    )
    if command_entity is None:
        return None

    command_length = _has_length(command_entity)
    if command_length is None or command_length > len(text):
        return None

    command_text = text[:command_length]
    parsed_command = parse_chat_command(command_text, bot_username=bot_username)
    if parsed_command is None:
        return None
    tail = text[command_entity.length :].strip()
    return parsed_command.name.lower(), tail.strip(), text.strip()


def _parse_command_from_text(
    text: str, bot_username: Optional[str]
) -> Optional[Tuple[str, str, str]]:
    parsed = parse_chat_command(text, bot_username=bot_username)
    if parsed is None:
        return None
    return parsed.name.lower(), parsed.args, parsed.raw
