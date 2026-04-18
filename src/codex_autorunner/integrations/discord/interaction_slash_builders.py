"""Slash command option builders, handler shims, and autocomplete choice builders.

Extracted from interaction_registry.py to reduce mixed ownership. The registry
route tables still reference these handlers, but the handler logic lives here.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Literal, Optional

from ...core.update_targets import update_target_command_choices
from ..chat.agents import (
    chat_agent_command_choices,
    chat_agent_description,
    chat_hermes_profile_options,
)
from ..chat.model_selection import (
    reasoning_effort_command_choices,
    reasoning_effort_description,
)

SUB_COMMAND = 1
SUB_COMMAND_GROUP = 2
STRING = 3
INTEGER = 4
BOOLEAN = 5

ROOT_COMMANDS: dict[str, str] = {
    "car": "Codex Autorunner commands",
    "pma": "Proactive Mode Agent commands",
    "flow": "Ticket flow commands",
}

GROUP_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("car", "session"): "Session management commands",
    ("car", "files"): "Manage file inbox/outbox",
    ("car", "admin"): "Admin and operator commands",
}


def _string_option(
    name: str,
    description: str,
    *,
    required: bool = False,
    autocomplete: bool = False,
    choices: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    option: dict[str, Any] = {
        "type": STRING,
        "name": name,
        "description": description,
        "required": required,
    }
    if autocomplete:
        option["autocomplete"] = True
    if choices is not None:
        option["choices"] = choices
    return option


def _integer_option(
    name: str,
    description: str,
    *,
    required: bool = False,
) -> dict[str, Any]:
    return {
        "type": INTEGER,
        "name": name,
        "description": description,
        "required": required,
    }


def _boolean_option(
    name: str,
    description: str,
    *,
    required: bool = False,
) -> dict[str, Any]:
    return {
        "type": BOOLEAN,
        "name": name,
        "description": description,
        "required": required,
    }


async def _dispatch_service_method(
    service: Any,
    route: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    options: dict[str, Any],
    method_name: str,
    include_channel_id: bool = False,
    include_guild_id: bool = False,
    include_user_id: bool = False,
    include_options: bool = False,
    workspace_requirement: Literal["none", "bound", "flow_read"] = "none",
    flow_read_action: Optional[str] = None,
    extra_kwargs_factory: Optional[
        Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]
    ] = None,
) -> None:
    kwargs: dict[str, Any] = {}

    if workspace_requirement == "bound":
        workspace_root = await service._require_bound_workspace(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        if workspace_root is None:
            return
        kwargs["workspace_root"] = workspace_root
    elif workspace_requirement == "flow_read":
        action = flow_read_action or route.canonical_path[-1]
        workspace_root = await service._resolve_workspace_for_flow_read(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            action=action,
        )
        if workspace_root is None:
            return
        kwargs["workspace_root"] = workspace_root

    if include_channel_id:
        kwargs["channel_id"] = channel_id
    if include_guild_id:
        kwargs["guild_id"] = guild_id
    if include_user_id:
        kwargs["user_id"] = user_id
    if include_options:
        kwargs["options"] = options

    if extra_kwargs_factory is not None:
        extra = extra_kwargs_factory(
            service,
            route,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            options=options,
            kwargs=kwargs,
        )
        extra = await extra if inspect.isawaitable(extra) else extra
        if extra is None:
            return
        kwargs.update(extra)

    handler = getattr(service, method_name)
    await handler(interaction_id, interaction_token, **kwargs)


async def _handle_bind_slash(
    service: Any,
    route: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    options: dict[str, Any],
) -> None:
    await _dispatch_service_method(
        service,
        route,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id=user_id,
        options=options,
        method_name="_handle_bind",
        include_channel_id=True,
        include_guild_id=True,
        include_options=True,
    )


def _interrupt_extra_kwargs(
    _service: Any,
    _route: Any,
    _interaction_id: str,
    _interaction_token: str,
    *,
    user_id: Optional[str],
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "source": "slash_command",
        "source_command": "car session interrupt",
        "source_user_id": user_id,
    }


async def _handle_pma_route(
    service: Any,
    route: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    options: dict[str, Any],
) -> None:
    method_name_by_path: dict[tuple[str, ...], str] = {
        ("pma", "on"): "_handle_pma_on",
        ("pma", "off"): "_handle_pma_off",
        ("pma", "status"): "_handle_pma_status",
    }
    method_name = method_name_by_path.get(route.canonical_path)
    if method_name is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Unknown PMA subcommand. Use on, off, or status.",
        )
        return
    if not service._config.pma_enabled:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA is disabled in hub config. Set pma.enabled: true to enable.",
        )
        return
    await _dispatch_service_method(
        service,
        route,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id=user_id,
        options=options,
        method_name=method_name,
        include_channel_id=True,
        include_guild_id=route.canonical_path == ("pma", "on"),
    )


async def _build_bind_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_bind_autocomplete_choices

    _ = channel_id, command_path, options
    return build_bind_autocomplete_choices(service, focused_value)


async def _build_model_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_model_autocomplete_choices

    _ = command_path, options
    return await build_model_autocomplete_choices(
        service,
        channel_id=channel_id,
        query=focused_value,
    )


async def _build_skills_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_skills_autocomplete_choices

    _ = command_path, options
    return await build_skills_autocomplete_choices(
        service,
        channel_id=channel_id,
        query=focused_value,
    )


async def _build_ticket_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_ticket_autocomplete_choices

    _ = command_path, options
    return await build_ticket_autocomplete_choices(
        service,
        channel_id=channel_id,
        query=focused_value,
    )


async def _build_session_resume_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_session_resume_autocomplete_choices

    _ = command_path, options
    return await build_session_resume_autocomplete_choices(
        service,
        channel_id=channel_id,
        query=focused_value,
    )


async def _build_flow_run_autocomplete_choices(
    service: Any,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_value: str,
) -> list[dict[str, str]]:
    from .car_autocomplete import build_flow_run_autocomplete_choices

    _ = options
    return await build_flow_run_autocomplete_choices(
        service,
        channel_id=channel_id,
        action=command_path[2],
        query=focused_value,
    )


def _bind_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "workspace",
            "Workspace path or repo id (optional - shows picker if omitted)",
            autocomplete=True,
        )
    ]


def _agent_options(context: Any) -> list[dict[str, Any]]:
    hermes_profile_choices = [
        {"name": option.profile, "value": option.profile}
        for option in chat_hermes_profile_options(context)
    ]
    return [
        _string_option(
            "name",
            f"Agent name: {chat_agent_description(context)}",
            choices=list(chat_agent_command_choices(context)),
        ),
        _string_option(
            "profile",
            "Hermes profile id (optional)",
            choices=hermes_profile_choices,
        ),
    ]


def _model_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "name",
            "Model name (e.g., gpt-5.4 or provider/model)",
            autocomplete=True,
        ),
        _string_option(
            "effort",
            (f"Reasoning effort (when supported): {reasoning_effort_description()}"),
            choices=list(reasoning_effort_command_choices()),
        ),
    ]


def _update_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "target",
            "Target: all, web, chat, telegram, discord, or status",
            choices=list(update_target_command_choices(include_status=True)),
        )
    ]


def _diff_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("path", "Optional path to diff")]


def _skills_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "search", "Optional search text to filter skills", autocomplete=True
        )
    ]


def _tickets_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "search", "Optional search text to filter tickets", autocomplete=True
        )
    ]


def _review_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "target",
            "Review target: uncommitted, base <branch>, commit <sha>, or custom",
        )
    ]


def _approvals_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "mode",
            "Mode: yolo, safe, read-only, auto, or full-access",
        )
    ]


def _mention_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option("path", "Path to the file to include", required=True),
        _string_option("request", "Optional request text"),
    ]


def _session_resume_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "thread_id",
            "Thread ID to resume (optional - lists recent threads if omitted)",
            autocomplete=True,
        )
    ]


def _files_clear_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option(
            "target",
            "inbox, outbox, or all (default: all)",
        )
    ]


def _feedback_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("reason", "Feedback reason/description", required=True)]


def _experimental_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option("action", "Action: list, enable, or disable"),
        _string_option("feature", "Feature name for enable/disable"),
    ]


def _flow_status_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("run_id", "Flow run id", autocomplete=True)]


def _flow_runs_options(_context: Any) -> list[dict[str, Any]]:
    return [_integer_option("limit", "Max runs (default 5)")]


def _flow_issue_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("issue_ref", "Issue number or URL", required=True)]


def _flow_plan_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("text", "Plan text", required=True)]


def _flow_start_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _boolean_option(
            "force_new",
            "Start a new run even if one is active/paused",
        )
    ]


def _flow_run_picker_options(_context: Any) -> list[dict[str, Any]]:
    return [_string_option("run_id", "Flow run id", autocomplete=True)]


def _flow_reply_options(_context: Any) -> list[dict[str, Any]]:
    return [
        _string_option("text", "Reply text", required=True),
        _string_option("run_id", "Flow run id", autocomplete=True),
    ]
