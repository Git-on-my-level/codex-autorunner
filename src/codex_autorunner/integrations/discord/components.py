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


def build_agent_picker(
    *,
    current_agent: str,
    custom_id: str = "agent_select",
    placeholder: str = "Select an agent...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label="codex",
            value="codex",
            description="Default Codex agent",
            default=current_agent == "codex",
        ),
        build_select_option(
            label="opencode",
            value="opencode",
            description="OpenCode agent (requires opencode binary)",
            default=current_agent == "opencode",
        ),
    ]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_model_picker(
    models: list[tuple[str, str]],
    *,
    current_model: Optional[str] = None,
    custom_id: str = "model_select",
    placeholder: str = "Select a model...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label="(default model)",
            value="clear",
            description="Clear model override",
            default=not current_model,
        )
    ]
    rendered_models: set[str] = set()
    option_limit = max(0, DISCORD_SELECT_OPTION_MAX_OPTIONS - 1)
    for model_id, label in models[:option_limit]:
        rendered_models.add(model_id)
        options.append(
            build_select_option(
                label=label,
                value=model_id,
                default=current_model == model_id,
            )
        )
    if current_model and current_model not in rendered_models:
        if len(options) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
            options.pop()
        options.append(
            build_select_option(
                label=f"{current_model} (current)",
                value=current_model,
                default=True,
            )
        )
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_model_effort_picker(
    *,
    custom_id: str = "model_effort_select",
    placeholder: str = "Select reasoning effort...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label="(none)",
            value="none",
            description="Do not set an effort override",
            default=True,
        ),
        build_select_option("minimal", "minimal"),
        build_select_option("low", "low"),
        build_select_option("medium", "medium"),
        build_select_option("high", "high"),
        build_select_option("xhigh", "xhigh"),
    ]
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


def build_session_threads_picker(
    threads: list[tuple[str, str]],
    *,
    custom_id: str = "session_resume_select",
    placeholder: str = "Select a thread to resume...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=label[:100],
            value=thread_id,
            description="Resume this thread",
        )
        for thread_id, label in threads[:DISCORD_SELECT_OPTION_MAX_OPTIONS]
    ]
    if not options:
        options = [build_select_option("No threads available", "none", default=True)]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_review_commit_picker(
    commits: list[tuple[str, str]],
    *,
    custom_id: str = "review_commit_select",
    placeholder: str = "Select a commit...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=f"{sha[:7]} - {subject}"[:100] if subject else sha[:7],
            value=sha,
            description=subject[:100] if subject else "Commit",
        )
        for sha, subject in commits[:DISCORD_SELECT_OPTION_MAX_OPTIONS]
    ]
    if not options:
        options = [build_select_option("No commits available", "none", default=True)]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_update_target_picker(
    *,
    custom_id: str = "update_target_select",
    placeholder: str = "Select update target...",
) -> dict[str, Any]:
    options = [
        build_select_option("both", "both", description="Web + Chat apps"),
        build_select_option("web", "web", description="Web UI only"),
        build_select_option("chat", "chat", description="Telegram + Discord"),
        build_select_option("telegram", "telegram", description="Telegram only"),
        build_select_option("discord", "discord", description="Discord only"),
        build_select_option("status", "status", description="Show update status"),
    ]
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
