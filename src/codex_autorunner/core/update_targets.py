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
_UPDATE_TARGET_STATUS = UpdateTargetDefinition(
    value="status",
    label="Status",
    description="Show update status",
    restart_notice="",
    includes_web=False,
)


def _format_service_list(services: tuple[str, ...]) -> str:
    if len(services) == 1:
        return services[0]
    if len(services) == 2:
        return f"{services[0]} and {services[1]}"
    return f"{', '.join(services[:-1])}, and {services[-1]}"


def _all_target_definition(
    *, telegram_available: bool, discord_available: bool
) -> UpdateTargetDefinition:
    services = ["Web"]
    restart_services = ["web UI"]
    if telegram_available:
        services.append("Telegram")
        restart_services.append("Telegram")
    if discord_available:
        services.append("Discord")
        restart_services.append("Discord")
    return UpdateTargetDefinition(
        value="both",
        label="All",
        description=" + ".join(services),
        restart_notice=f"The {_format_service_list(tuple(restart_services))} will restart.",
        includes_web=True,
    )


def all_update_target_definitions() -> tuple[UpdateTargetDefinition, ...]:
    return tuple(_UPDATE_TARGET_DEFINITIONS[key] for key in _UPDATE_TARGET_ORDER)


def update_target_label_pairs(
    definitions: tuple[UpdateTargetDefinition, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    items = definitions if definitions is not None else all_update_target_definitions()
    return tuple((definition.value, definition.label) for definition in items)


def update_target_values(*, include_status: bool = False) -> tuple[str, ...]:
    values = tuple(definition.value for definition in all_update_target_definitions())
    if not include_status:
        return values
    return (*values, _UPDATE_TARGET_STATUS.value)


def update_target_command_choices(
    *, include_status: bool = False
) -> tuple[dict[str, str], ...]:
    choices = tuple(
        {
            "name": definition.label,
            "value": "all" if definition.value == "both" else definition.value,
        }
        for definition in all_update_target_definitions()
    )
    if not include_status:
        return choices
    return (
        *choices,
        {
            "name": _UPDATE_TARGET_STATUS.label,
            "value": _UPDATE_TARGET_STATUS.value,
        },
    )


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
    definitions: list[UpdateTargetDefinition] = []
    if telegram_available or discord_available:
        definitions.append(
            _all_target_definition(
                telegram_available=telegram_available,
                discord_available=discord_available,
            )
        )
    definitions.append(_UPDATE_TARGET_DEFINITIONS["web"])
    if telegram_available and discord_available:
        definitions.append(_UPDATE_TARGET_DEFINITIONS["chat"])
    if telegram_available:
        definitions.append(_UPDATE_TARGET_DEFINITIONS["telegram"])
    if discord_available:
        definitions.append(_UPDATE_TARGET_DEFINITIONS["discord"])
    return tuple(definitions)
