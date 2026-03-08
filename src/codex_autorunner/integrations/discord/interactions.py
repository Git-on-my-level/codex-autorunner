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

    path, current_options = _extract_command_path_and_leaf_options(data)
    if not path:
        return (), {}

    parsed_options: dict[str, Any] = {}
    for item in current_options:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        parsed_options[name] = item.get("value")

    return tuple(path), parsed_options


def extract_autocomplete_command_context(
    interaction_payload: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, Any], Optional[str], str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return (), {}, None, ""

    path, current_options = _extract_command_path_and_leaf_options(data)
    if not path:
        return (), {}, None, ""

    parsed_options: dict[str, Any] = {}
    focused_name: Optional[str] = None
    focused_value = ""

    for item in current_options:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        value = item.get("value")
        parsed_options[name] = value

        if bool(item.get("focused")):
            focused_name = name
            focused_value = str(value) if value is not None else ""

    return tuple(path), parsed_options, focused_name, focused_value


def _extract_command_path_and_leaf_options(
    data: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    root_name = data.get("name")
    if not isinstance(root_name, str) or not root_name:
        return [], []

    path = [root_name]
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
    normalized_options = [item for item in current_options if isinstance(item, dict)]
    return path, normalized_options


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


def is_autocomplete_interaction(interaction_payload: dict[str, Any]) -> bool:
    interaction_type = interaction_payload.get("type")
    return interaction_type == 4


def is_modal_submit_interaction(interaction_payload: dict[str, Any]) -> bool:
    interaction_type = interaction_payload.get("type")
    return interaction_type == 5


def extract_component_custom_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return None
    return _as_id(data.get("custom_id"))


def extract_modal_custom_id(interaction_payload: dict[str, Any]) -> Optional[str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return None
    return _as_id(data.get("custom_id"))


def extract_modal_values(interaction_payload: dict[str, Any]) -> dict[str, Any]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return {}
    components = data.get("components")
    if not isinstance(components, list):
        return {}
    values: dict[str, Any] = {}
    for container in components:
        if not isinstance(container, dict):
            continue
        # Modal components can arrive in either legacy action-row form
        # (`components: [...]`) or label form (`component: {...}`).
        candidates: list[dict[str, Any]] = []
        label_component = container.get("component")
        if isinstance(label_component, dict):
            candidates.append(label_component)
        row_components = container.get("components")
        if isinstance(row_components, list):
            candidates.extend(item for item in row_components if isinstance(item, dict))
        for item in candidates:
            custom_id_raw = item.get("custom_id")
            if not isinstance(custom_id_raw, str):
                continue
            custom_id = custom_id_raw
            if item.get("type") == 4:
                value = item.get("value")
                if isinstance(value, str):
                    values[custom_id] = value
            if item.get("type") == 3:
                selected = item.get("values")
                if isinstance(selected, list):
                    values[custom_id] = [
                        str(v) for v in selected if isinstance(v, (str, int, float))
                    ]
    return values


def extract_component_values(interaction_payload: dict[str, Any]) -> list[str]:
    data = interaction_payload.get("data")
    if not isinstance(data, dict):
        return []
    values = data.get("values")
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, (str, int, float))]
