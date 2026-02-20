"""Shared command parsing helpers for Telegram adapters."""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence, Tuple

from .api_schemas import TelegramMessageEntitySchema

_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


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
    if not command_text.startswith("/"):
        return None
    command = command_text.lstrip("/")
    if not command:
        return None
    tail = text[command_entity.length :].strip()
    if "@" in command:
        name, _, target = command.partition("@")
        if bot_username and target.lower() != bot_username.lower():
            return None
        command = name
    return command.lower(), tail.strip(), text.strip()


def _parse_command_from_text(
    text: str, bot_username: Optional[str]
) -> Optional[Tuple[str, str, str]]:
    trimmed = text.strip()
    if not trimmed.startswith("/"):
        return None

    parts = trimmed.split(None, 1)
    command = parts[0].lstrip("/")
    if not command:
        return None
    if "@" in command:
        name, _, target = command.partition("@")
        if bot_username and target.lower() != bot_username.lower():
            return None
        command = name
    if not command or not _COMMAND_NAME_RE.fullmatch(command):
        return None

    args = parts[1].strip() if len(parts) > 1 else ""
    return command.lower(), args, trimmed
