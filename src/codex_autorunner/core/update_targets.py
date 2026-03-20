from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class UpdateTargetDefinition:
    value: str
    label: str
    description: str
    restart_notice: str
    includes_web: bool


_DEFAULT_UPDATE_TARGET = "both"
_UPDATE_TARGET_ORDER = ("both", "web", "chat", "telegram", "discord")
_UPDATE_TARGET_DEFINITIONS = {
    "both": UpdateTargetDefinition(
        value="both",
        label="All",
        description="Web + Telegram + Discord",
        restart_notice="The web UI, Telegram, and Discord will restart.",
        includes_web=True,
    ),
    "web": UpdateTargetDefinition(
        value="web",
        label="Web only",
        description="Web UI only",
        restart_notice="The web UI will restart.",
        includes_web=True,
    ),
    "chat": UpdateTargetDefinition(
        value="chat",
        label="Chat apps (Telegram + Discord)",
        description="Telegram + Discord",
        restart_notice="Telegram and Discord will restart.",
        includes_web=False,
    ),
    "telegram": UpdateTargetDefinition(
        value="telegram",
        label="Telegram only",
        description="Telegram only",
        restart_notice="Telegram will restart.",
        includes_web=False,
    ),
    "discord": UpdateTargetDefinition(
        value="discord",
        label="Discord only",
        description="Discord only",
        restart_notice="Discord will restart.",
        includes_web=False,
    ),
}
_UPDATE_TARGET_ALIASES = {
    "": _DEFAULT_UPDATE_TARGET,
    "all": "both",
    "both": "both",
    "web": "web",
    "hub": "web",
    "server": "web",
    "ui": "web",
    "chat": "chat",
    "chat-apps": "chat",
    "apps": "chat",
    "telegram": "telegram",
    "tg": "telegram",
    "bot": "telegram",
    "discord": "discord",
    "dc": "discord",
}


def all_update_target_definitions() -> tuple[UpdateTargetDefinition, ...]:
    return tuple(_UPDATE_TARGET_DEFINITIONS[key] for key in _UPDATE_TARGET_ORDER)


def normalize_update_target(raw: Optional[str]) -> str:
    if raw is None:
        return _DEFAULT_UPDATE_TARGET
    value = str(raw).strip().lower()
    normalized = _UPDATE_TARGET_ALIASES.get(value)
    if normalized is not None:
        return normalized
    raise ValueError(
        "Unsupported update target (use all, web, chat, telegram, or discord)."
    )


def get_update_target_definition(raw: Optional[str]) -> UpdateTargetDefinition:
    return _UPDATE_TARGET_DEFINITIONS[normalize_update_target(raw)]


def get_update_target_label(raw: Optional[str]) -> str:
    return get_update_target_definition(raw).label


def available_update_target_definitions(
    *,
    telegram_available: bool,
    discord_available: bool,
) -> tuple[UpdateTargetDefinition, ...]:
    values: list[str] = []
    if telegram_available or discord_available:
        values.append("both")
    values.append("web")
    if telegram_available and discord_available:
        values.append("chat")
    if telegram_available:
        values.append("telegram")
    if discord_available:
        values.append("discord")
    return tuple(_UPDATE_TARGET_DEFINITIONS[value] for value in values)
