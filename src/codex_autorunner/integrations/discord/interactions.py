from __future__ import annotations

from typing import Any, Optional


def _as_id(value: object) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def extract_command_path_and_options(
    interaction_payload: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, Any]]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return (), {}

    root_name = data.get("name")
    if not isinstance(root_name, str) or not root_name:
        return (), {}

    path: list[str] = [root_name]
    options = data.get("options")
    current_options = options if isinstance(options, list) else []

    while current_options:
        first = current_options[0]
        if not isinstance(first, dict):
            break
        option_type = first.get("type")
        if option_type not in (1, 2):
            break
        name = first.get("name")
        if isinstance(name, str) and name:
            path.append(name)
        nested = first.get("options")
        current_options = nested if isinstance(nested, list) else []

    parsed_options: dict[str, Any] = {}
    for item in current_options:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        parsed_options[name] = item.get("value")

    return tuple(path), parsed_options


def extract_interaction_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    return _as_id(interaction_payload.get("id"))


def extract_interaction_token(interaction_payload: dict[str, Any]) -> Optional[str]:
    return _as_id(interaction_payload.get("token"))


def extract_channel_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    return _as_id(interaction_payload.get("channel_id"))


def extract_guild_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    return _as_id(interaction_payload.get("guild_id"))


def extract_user_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    member = interaction_payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            user_id = _as_id(member_user.get("id"))
            if user_id:
                return user_id
    user = interaction_payload.get("user")
    if isinstance(user, dict):
        return _as_id(user.get("id"))
    return None


def is_component_interaction(interaction_payload: dict[str, Any]) -> bool:
    interaction_type = interaction_payload.get("type")
    return interaction_type == 3


def extract_component_custom_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return None
    return _as_id(data.get("custom_id"))


def extract_component_values(interaction_payload: dict[str, Any]) -> list[str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return []
    values = data.get("values")
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, (str, int, float))]
