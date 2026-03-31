from __future__ import annotations

from typing import Any, Optional, Sequence

from ...core.update_targets import (
    UpdateTargetDefinition,
    all_update_target_definitions,
)
from ..chat.agents import chat_agent_definitions
from ..chat.model_selection import REASONING_EFFORT_VALUES

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
    workspaces: list[tuple[str, str] | tuple[str, str, Optional[str]]],
    *,
    custom_id: str = "bind_select",
    placeholder: str = "Select a workspace...",
) -> dict[str, Any]:
    options = []
    for entry in workspaces[:DISCORD_SELECT_OPTION_MAX_OPTIONS]:
        if len(entry) == 2:
            value, label = entry
            description = None
        else:
            value, label, description = entry
        options.append(
            build_select_option(
                label=label[:100],
                value=value,
                description=description[:100] if description else None,
            )
        )
    if not options:
        options = [build_select_option("No workspaces available", "none", default=True)]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_agent_picker(
    *,
    current_agent: str,
    context: Any = None,
    custom_id: str = "agent_select",
    placeholder: str = "Select an agent...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=definition.value,
            value=definition.value,
            description=definition.description,
            default=current_agent == definition.value,
        )
        for definition in chat_agent_definitions(context)
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
    options = []
    for effort in REASONING_EFFORT_VALUES:
        if effort == "none":
            options.append(
                build_select_option(
                    label="(none)",
                    value=effort,
                    description="Do not set an effort override",
                    default=True,
                )
            )
            continue
        options.append(build_select_option(effort, effort))
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
    current_run_id: Optional[str] = None,
) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    rendered_run_ids: set[str] = set()
    option_limit = DISCORD_SELECT_OPTION_MAX_OPTIONS

    for run_id, status in runs[:option_limit]:
        rendered_run_ids.add(run_id)
        options.append(
            build_select_option(
                label=f"{run_id[:50]} [{status}]"[:100],
                value=run_id,
                description=f"Status: {status}",
                default=current_run_id == run_id,
            )
        )

    if current_run_id and current_run_id not in rendered_run_ids:
        current_entry = next(
            (entry for entry in runs if entry[0] == current_run_id),
            None,
        )
        if current_entry is not None:
            run_id, status = current_entry
            if len(options) >= option_limit:
                options.pop()
            options.append(
                build_select_option(
                    label=f"{run_id[:50]} [{status}]"[:100],
                    value=run_id,
                    description=f"Status: {status}",
                    default=True,
                )
            )

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
    target_definitions: Optional[Sequence[UpdateTargetDefinition]] = None,
    custom_id: str = "update_target_select",
    placeholder: str = "Select update target...",
) -> dict[str, Any]:
    definitions = (
        tuple(target_definitions)
        if target_definitions is not None
        else all_update_target_definitions()
    )
    options = [
        build_select_option(
            definition.label,
            definition.value,
            description=definition.description,
        )
        for definition in definitions[: DISCORD_SELECT_OPTION_MAX_OPTIONS - 1]
    ]
    options.append(
        build_select_option("status", "status", description="Show update status")
    )
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_ticket_filter_picker(
    *,
    current_filter: str,
    custom_id: str = "tickets_filter_select",
    placeholder: str = "Filter tickets...",
) -> dict[str, Any]:
    normalized = current_filter.strip().lower() if current_filter else "all"
    if normalized not in {"all", "open", "done"}:
        normalized = "all"
    options = [
        build_select_option(
            label="open",
            value="open",
            description="Only not completed tickets",
            default=normalized == "open",
        ),
        build_select_option(
            label="done",
            value="done",
            description="Only completed tickets",
            default=normalized == "done",
        ),
        build_select_option(
            label="all",
            value="all",
            description="All tickets",
            default=normalized == "all",
        ),
    ]
    return build_action_row(
        [build_select_menu(custom_id, options, placeholder=placeholder)]
    )


def build_ticket_picker(
    tickets: list[tuple[str, str, str]],
    *,
    custom_id: str = "tickets_select",
    placeholder: str = "Select a ticket...",
) -> dict[str, Any]:
    options = [
        build_select_option(
            label=label[:100],
            value=ticket_id,
            description=description[:100] if description else None,
        )
        for ticket_id, label, description in tickets[:DISCORD_SELECT_OPTION_MAX_OPTIONS]
    ]
    if not options:
        options = [build_select_option("No tickets found", "none", default=True)]
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


def build_queue_notice_buttons(source_message_id: str) -> dict[str, Any]:
    source = str(source_message_id or "").strip()
    if not source:
        raise ValueError("source_message_id required")
    return build_action_row(
        [
            build_button(
                "Cancel",
                f"queue_cancel:{source}",
                style=DISCORD_BUTTON_STYLE_DANGER,
            ),
            build_button(
                "Interrupt + Send",
                f"queue_interrupt_send:{source}",
                style=DISCORD_BUTTON_STYLE_PRIMARY,
            ),
        ]
    )
