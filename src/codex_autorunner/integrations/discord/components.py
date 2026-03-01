from __future__ import annotations

from typing import Any, Optional

DISCORD_BUTTON_STYLE_PRIMARY = 1
DISCORD_BUTTON_STYLE_SECONDARY = 2
DISCORD_BUTTON_STYLE_SUCCESS = 3
DISCORD_BUTTON_STYLE_DANGER = 4
DISCORD_BUTTON_STYLE_LINK = 5
DISCORD_SELECT_OPTION_MAX_OPTIONS = 25


def build_action_row(components: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": 1,
        "components": components,
    }


def build_button(
    label: str,
    custom_id: str,
    *,
    style: int = DISCORD_BUTTON_STYLE_SECONDARY,
    emoji: Optional[str] = None,
    disabled: bool = False,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": 2,
        "style": style,
        "label": label,
        "custom_id": custom_id,
        "disabled": disabled,
    }
    if emoji:
        button["emoji"] = {"name": emoji}
    return button


def build_select_menu(
    custom_id: str,
    options: list[dict[str, Any]],
    *,
    placeholder: Optional[str] = None,
    min_values: int = 1,
    max_values: int = 1,
    disabled: bool = False,
) -> dict[str, Any]:
    select: dict[str, Any] = {
        "type": 3,
        "custom_id": custom_id,
        "options": options[:DISCORD_SELECT_OPTION_MAX_OPTIONS],
        "min_values": min_values,
        "max_values": min(max_values, DISCORD_SELECT_OPTION_MAX_OPTIONS),
        "disabled": disabled,
    }
    if placeholder:
        select["placeholder"] = placeholder[:100]
    return select


def build_select_option(
    label: str,
    value: str,
    *,
    description: Optional[str] = None,
    emoji: Optional[str] = None,
    default: bool = False,
) -> dict[str, Any]:
    option: dict[str, Any] = {
        "label": label[:100],
        "value": value[:100],
        "default": default,
    }
    if description:
        option["description"] = description[:100]
    if emoji:
        option["emoji"] = {"name": emoji}
    return option


def build_bind_picker(
    repos: list[tuple[str, str]],
    *,
    custom_id: str = "bind_select",
    placeholder: str = "Select a workspace...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=repo_id[:100],
            value=repo_id,
            description=path[:100] if path else None,
        )
        for repo_id, path in repos[:DISCORD_SELECT_OPTION_MAX_OPTIONS]
    ]
    if not options:
        options = [build_select_option("No repos available", "none", default=True)]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_flow_status_buttons(
    run_id: str,
    status: str,
    *,
    include_refresh: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []

    if status == "paused":
        buttons.append(
            build_button(
                "Resume",
                f"flow:{run_id}:resume",
                style=DISCORD_BUTTON_STYLE_SUCCESS,
            )
        )
        buttons.append(
            build_button(
                "Restart",
                f"flow:{run_id}:restart",
                style=DISCORD_BUTTON_STYLE_SECONDARY,
            )
        )
        rows.append(build_action_row(buttons))
        buttons = []
        buttons.append(
            build_button(
                "Archive",
                f"flow:{run_id}:archive",
                style=DISCORD_BUTTON_STYLE_SECONDARY,
            )
        )
    elif status in {"completed", "stopped", "failed"}:
        buttons.append(
            build_button(
                "Restart",
                f"flow:{run_id}:restart",
                style=DISCORD_BUTTON_STYLE_SECONDARY,
            )
        )
        buttons.append(
            build_button(
                "Archive",
                f"flow:{run_id}:archive",
                style=DISCORD_BUTTON_STYLE_SECONDARY,
            )
        )
        if include_refresh:
            buttons.append(
                build_button(
                    "Refresh",
                    f"flow:{run_id}:refresh",
                    style=DISCORD_BUTTON_STYLE_SECONDARY,
                )
            )
    else:
        if include_refresh:
            buttons.append(
                build_button(
                    "Stop",
                    f"flow:{run_id}:stop",
                    style=DISCORD_BUTTON_STYLE_DANGER,
                )
            )
            buttons.append(
                build_button(
                    "Refresh",
                    f"flow:{run_id}:refresh",
                    style=DISCORD_BUTTON_STYLE_SECONDARY,
                )
            )

    if buttons:
        rows.append(build_action_row(buttons))

    return rows


def build_flow_runs_picker(
    runs: list[tuple[str, str]],
    *,
    custom_id: str = "flow_runs_select",
    placeholder: str = "Select a run...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=f"{run_id[:50]} [{status}]"[:100],
            value=run_id,
            description=f"Status: {status}",
        )
        for run_id, status in runs[:DISCORD_SELECT_OPTION_MAX_OPTIONS]
    ]
    if not options:
        options = [build_select_option("No runs available", "none", default=True)]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_cancel_turn_button(
    *,
    custom_id: str = "cancel_turn",
) -> dict[str, Any]:
    return build_action_row(
        [
            build_button(
                "Cancel",
                custom_id,
                style=DISCORD_BUTTON_STYLE_DANGER,
            )
        ]
    )


def build_continue_turn_button(
    *,
    custom_id: str = "continue_turn",
) -> dict[str, Any]:
    return build_action_row(
        [
            build_button(
                "Continue",
                custom_id,
                style=DISCORD_BUTTON_STYLE_SUCCESS,
            )
        ]
    )
