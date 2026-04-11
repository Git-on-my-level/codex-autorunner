from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from ...core.flows.catalog import FLOW_ACTION_SPECS, FLOW_ACTIONS_WITH_RUN_PICKER
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
from .interaction_runtime import (
    ensure_component_response_deferred,
    ensure_ephemeral_response_deferred,
)

DiscordAckPolicy = Literal[
    "immediate",
    "defer_ephemeral",
    "defer_public",
    "defer_component_update",
]
DiscordAckTiming = Literal["dispatch", "post_private_preflight"]
DiscordExposure = Literal["public", "operator"]
SchedulerAckStrategy = Literal[
    "none",
    "scheduler_component_update",
    "scheduler_ephemeral",
]
WorkspaceLockPolicy = Literal[
    "none",
    "bound_workspace",
    "bind_target_workspace",
]

# Discord application command option types.
SUB_COMMAND = 1
SUB_COMMAND_GROUP = 2
STRING = 3
INTEGER = 4
BOOLEAN = 5

BIND_SELECT_CUSTOM_ID = "bind_select"
BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"
FLOW_RUNS_SELECT_ID = "flow_runs_select"
AGENT_SELECT_CUSTOM_ID = "agent_select"
AGENT_PROFILE_SELECT_ID = "agent_profile_select"
MODEL_SELECT_CUSTOM_ID = "model_select"
MODEL_EFFORT_SELECT_ID = "model_effort_select"
SESSION_RESUME_SELECT_ID = "session_resume_select"
UPDATE_TARGET_SELECT_ID = "update_target_select"
UPDATE_CONFIRM_PREFIX = "update_confirm"
UPDATE_CANCEL_PREFIX = "update_cancel"
REVIEW_COMMIT_SELECT_ID = "review_commit_select"
FLOW_ACTION_SELECT_PREFIX = "flow_action_select"
TICKETS_FILTER_SELECT_ID = "tickets_filter_select"
TICKETS_SELECT_ID = "tickets_select"
TICKETS_MODAL_PREFIX = "tickets_modal"
NEWT_HARD_RESET_CUSTOM_ID = "newt_hard_reset"
NEWT_CANCEL_CUSTOM_ID = "newt_cancel"

FLOW_COMPONENT_PREPARE_ACTIONS = frozenset(
    {
        "archive",
        "archive_cancel",
        "archive_cancel_prompt",
        "archive_confirm",
        "archive_confirm_prompt",
        "refresh",
        "restart",
    }
)

SlashOptionsFactory = Callable[[Any], list[dict[str, Any]]]
SlashHandler = Callable[..., Awaitable[None]]
ComponentHandler = Callable[[Any, Any], Awaitable[None]]
ComponentPrepare = Callable[[Any, Any], Awaitable[bool]]
AutocompleteChoicesBuilder = Callable[..., Awaitable[list[dict[str, str]]]]
ModalHandler = Callable[[Any, Any], Awaitable[None]]


@dataclass(frozen=True)
class SlashCommandRoute:
    id: str
    canonical_path: tuple[str, ...]
    registered_path: tuple[str, ...]
    description: str
    handler: SlashHandler
    options_factory: Optional[SlashOptionsFactory] = None
    ack_policy: Optional[DiscordAckPolicy] = None
    ack_timing: DiscordAckTiming = "dispatch"
    exposure: Optional[DiscordExposure] = None
    requires_workspace: bool = False
    required_capabilities: tuple[str, ...] = ()
    include_in_payload: bool = True
    catalog_in_contract: bool = True
    workspace_lock_policy: WorkspaceLockPolicy = "none"
    group_description: Optional[str] = None


@dataclass(frozen=True)
class ComponentRoute:
    id: str
    handler: ComponentHandler
    exact_custom_id: Optional[str] = None
    custom_id_prefix: Optional[str] = None
    scheduler_ack_strategy: SchedulerAckStrategy = "scheduler_component_update"
    workspace_lock_policy: WorkspaceLockPolicy = "none"
    prepare: Optional[ComponentPrepare] = None

    def matches(self, custom_id: str) -> bool:
        if self.exact_custom_id is not None:
            return custom_id == self.exact_custom_id
        if self.custom_id_prefix is not None:
            return custom_id.startswith(self.custom_id_prefix)
        return False


@dataclass(frozen=True)
class ModalRoute:
    id: str
    handler: ModalHandler
    exact_custom_id: Optional[str] = None
    custom_id_prefix: Optional[str] = None
    scheduler_ack_strategy: SchedulerAckStrategy = "scheduler_ephemeral"
    workspace_lock_policy: WorkspaceLockPolicy = "bound_workspace"

    def matches(self, custom_id: str) -> bool:
        if self.exact_custom_id is not None:
            return custom_id == self.exact_custom_id
        if self.custom_id_prefix is not None:
            return custom_id.startswith(self.custom_id_prefix)
        return False


@dataclass(frozen=True)
class AutocompleteRoute:
    id: str
    command_path: tuple[str, ...]
    focused_name: str
    choices_builder: AutocompleteChoicesBuilder

    def matches(
        self,
        *,
        command_path: tuple[str, ...],
        focused_name: Optional[str],
    ) -> bool:
        return command_path == self.command_path and focused_name == self.focused_name


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
    route: SlashCommandRoute,
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
    route: SlashCommandRoute,
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
    _route: SlashCommandRoute,
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
    route: SlashCommandRoute,
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
            ("Reasoning effort (when supported): " f"{reasoning_effort_description()}"),
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

_SLASH_ROUTES: tuple[SlashCommandRoute, ...] = (
    SlashCommandRoute(
        id="car.bind",
        canonical_path=("car", "bind"),
        registered_path=("car", "bind"),
        description="Bind channel to workspace",
        handler=_handle_bind_slash,
        options_factory=_bind_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        required_capabilities=(),
        workspace_lock_policy="bind_target_workspace",
    ),
    SlashCommandRoute(
        id="car.status",
        canonical_path=("car", "status"),
        registered_path=("car", "status"),
        description="Show binding status and active session info",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_status",
            include_channel_id=True,
            include_guild_id=True,
            include_user_id=True,
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
    ),
    SlashCommandRoute(
        id="car.new",
        canonical_path=("car", "new"),
        registered_path=("car", "new"),
        description="Start a fresh chat session for this channel",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_new",
            include_channel_id=True,
        ),
        ack_policy="defer_public",
        exposure="public",
        requires_workspace=True,
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.newt",
        canonical_path=("car", "newt"),
        registered_path=("car", "newt"),
        description="Reset branch from origin default branch and start a fresh chat session",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_newt",
            include_channel_id=True,
            include_guild_id=True,
        ),
        ack_policy="defer_public",
        exposure="public",
        requires_workspace=True,
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.agent",
        canonical_path=("car", "agent"),
        registered_path=("car", "agent"),
        description="View or set the agent",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_agent",
            include_channel_id=True,
            include_options=True,
        ),
        options_factory=_agent_options,
        ack_policy="immediate",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("agent_selection",),
    ),
    SlashCommandRoute(
        id="car.model",
        canonical_path=("car", "model"),
        registered_path=("car", "model"),
        description="View or set the model",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_model",
            include_channel_id=True,
            include_user_id=True,
            include_options=True,
        ),
        options_factory=_model_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("model_selection",),
    ),
    SlashCommandRoute(
        id="car.update",
        canonical_path=("car", "update"),
        registered_path=("car", "update"),
        description="Update CAR service",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_update",
            include_channel_id=True,
            include_options=True,
        ),
        options_factory=_update_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        required_capabilities=("service_update",),
    ),
    SlashCommandRoute(
        id="car.diff",
        canonical_path=("car", "diff"),
        registered_path=("car", "diff"),
        description="Show git diff",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_diff",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_diff_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
    ),
    SlashCommandRoute(
        id="car.skills",
        canonical_path=("car", "skills"),
        registered_path=("car", "skills"),
        description="List available skills",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_skills",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_skills_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
    ),
    SlashCommandRoute(
        id="car.tickets",
        canonical_path=("car", "tickets"),
        registered_path=("car", "tickets"),
        description="Manage tickets via modal",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_tickets",
            include_channel_id=True,
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_tickets_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
    ),
    SlashCommandRoute(
        id="car.review",
        canonical_path=("car", "review"),
        registered_path=("car", "review"),
        description="Run a code review",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_review",
            include_channel_id=True,
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_review_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("review",),
    ),
    SlashCommandRoute(
        id="car.approvals",
        canonical_path=("car", "approvals"),
        registered_path=("car", "approvals"),
        description="Set approval and sandbox policy",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_approvals",
            include_channel_id=True,
            include_options=True,
        ),
        options_factory=_approvals_options,
        ack_policy="immediate",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("approval_policy", "sandbox_policy"),
    ),
    SlashCommandRoute(
        id="car.mention",
        canonical_path=("car", "mention"),
        registered_path=("car", "mention"),
        description="Include a file in a new request",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_mention",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_mention_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("file_mentions",),
    ),
    SlashCommandRoute(
        id="car.archive",
        canonical_path=("car", "archive"),
        registered_path=("car", "archive"),
        description="Archive workspace state for a fresh start",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_archive",
            include_channel_id=True,
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
    ),
    SlashCommandRoute(
        id="car.resume",
        canonical_path=("car", "session", "resume"),
        registered_path=("car", "session", "resume"),
        description="Resume a previous chat thread",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_resume",
            include_channel_id=True,
            include_options=True,
        ),
        options_factory=_session_resume_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("durable_threads",),
        group_description=GROUP_DESCRIPTIONS[("car", "session")],
    ),
    SlashCommandRoute(
        id="car.reset",
        canonical_path=("car", "session", "reset"),
        registered_path=("car", "session", "reset"),
        description="Reset PMA thread state (clear volatile state)",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_reset",
            include_channel_id=True,
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("durable_threads",),
        group_description=GROUP_DESCRIPTIONS[("car", "session")],
    ),
    SlashCommandRoute(
        id="car.compact",
        canonical_path=("car", "session", "compact"),
        registered_path=("car", "session", "compact"),
        description="Compact the conversation (summary)",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_compact",
            include_channel_id=True,
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("message_turns",),
        group_description=GROUP_DESCRIPTIONS[("car", "session")],
    ),
    SlashCommandRoute(
        id="car.interrupt",
        canonical_path=("car", "session", "interrupt"),
        registered_path=("car", "session", "interrupt"),
        description="Stop the active turn",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_interrupt",
            include_channel_id=True,
            extra_kwargs_factory=_interrupt_extra_kwargs,
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("interrupt",),
        group_description=GROUP_DESCRIPTIONS[("car", "session")],
    ),
    SlashCommandRoute(
        id="car.logout",
        canonical_path=("car", "session", "logout"),
        registered_path=("car", "session", "logout"),
        description="Log out of the Codex account",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_logout",
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        required_capabilities=("auth_session",),
        group_description=GROUP_DESCRIPTIONS[("car", "session")],
    ),
    SlashCommandRoute(
        id="car.files.inbox",
        canonical_path=("car", "files", "inbox"),
        registered_path=("car", "files", "inbox"),
        description="List files in inbox",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_files_inbox",
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("filebox_access",),
        group_description=GROUP_DESCRIPTIONS[("car", "files")],
    ),
    SlashCommandRoute(
        id="car.files.outbox",
        canonical_path=("car", "files", "outbox"),
        registered_path=("car", "files", "outbox"),
        description="List pending outbox files",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_files_outbox",
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("filebox_access",),
        group_description=GROUP_DESCRIPTIONS[("car", "files")],
    ),
    SlashCommandRoute(
        id="car.files.clear",
        canonical_path=("car", "files", "clear"),
        registered_path=("car", "files", "clear"),
        description="Clear inbox/outbox files",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_files_clear",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_files_clear_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("filebox_access",),
        group_description=GROUP_DESCRIPTIONS[("car", "files")],
    ),
    SlashCommandRoute(
        id="car.help",
        canonical_path=("car", "help"),
        registered_path=("car", "admin", "help"),
        description="Show available commands",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_help",
        ),
        ack_policy="immediate",
        exposure="operator",
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.debug",
        canonical_path=("car", "debug"),
        registered_path=("car", "admin", "debug"),
        description="Show debug info for troubleshooting",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_debug",
            include_channel_id=True,
            include_guild_id=True,
            include_user_id=True,
        ),
        ack_policy="defer_ephemeral",
        exposure="operator",
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.ids",
        canonical_path=("car", "ids"),
        registered_path=("car", "admin", "ids"),
        description="Show channel/user IDs for debugging",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_ids",
            include_channel_id=True,
            include_guild_id=True,
            include_user_id=True,
        ),
        ack_policy="immediate",
        exposure="operator",
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.init",
        canonical_path=("car", "init"),
        registered_path=("car", "admin", "init"),
        description="Generate AGENTS.md",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_init",
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="operator",
        requires_workspace=True,
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.repos",
        canonical_path=("car", "repos"),
        registered_path=("car", "admin", "repos"),
        description="List hub repositories",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_repos",
        ),
        ack_policy="immediate",
        exposure="operator",
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.rollout",
        canonical_path=("car", "rollout"),
        registered_path=("car", "admin", "rollout"),
        description="Show current thread rollout path",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_rollout",
            include_channel_id=True,
        ),
        ack_policy="immediate",
        exposure="operator",
        requires_workspace=True,
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.feedback",
        canonical_path=("car", "feedback"),
        registered_path=("car", "admin", "feedback"),
        description="Send feedback and logs",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_feedback",
            include_channel_id=True,
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_feedback_options,
        ack_policy="defer_ephemeral",
        exposure="operator",
        required_capabilities=("feedback_reporting",),
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.mcp",
        canonical_path=("car", "mcp"),
        registered_path=("car", "admin", "mcp"),
        description="Inspect MCP server status",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_mcp",
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="operator",
        include_in_payload=False,
        catalog_in_contract=False,
        requires_workspace=True,
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="car.experimental",
        canonical_path=("car", "experimental"),
        registered_path=("car", "admin", "experimental"),
        description="Manage experimental feature flags",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_car_experimental",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_experimental_options,
        ack_policy="immediate",
        exposure="operator",
        include_in_payload=False,
        catalog_in_contract=False,
        requires_workspace=True,
        required_capabilities=("feature_flags",),
        group_description=GROUP_DESCRIPTIONS[("car", "admin")],
    ),
    SlashCommandRoute(
        id="pma.on",
        canonical_path=("pma", "on"),
        registered_path=("pma", "on"),
        description="Enable PMA mode for this channel",
        handler=_handle_pma_route,
        ack_policy="immediate",
        exposure="public",
        required_capabilities=("pma_mode",),
    ),
    SlashCommandRoute(
        id="pma.off",
        canonical_path=("pma", "off"),
        registered_path=("pma", "off"),
        description="Disable PMA mode and restore previous binding",
        handler=_handle_pma_route,
        ack_policy="immediate",
        exposure="public",
        required_capabilities=("pma_mode",),
    ),
    SlashCommandRoute(
        id="pma.status",
        canonical_path=("pma", "status"),
        registered_path=("pma", "status"),
        description="Show PMA mode status",
        handler=_handle_pma_route,
        ack_policy="immediate",
        exposure="public",
        required_capabilities=("pma_mode",),
    ),
    SlashCommandRoute(
        id="car.flow.status",
        canonical_path=("car", "flow", "status"),
        registered_path=("flow", "status"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "status"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_status",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="flow_read",
            flow_read_action="status",
        ),
        options_factory=_flow_status_options,
        ack_policy="defer_public",
        exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    SlashCommandRoute(
        id="car.flow.runs",
        canonical_path=("car", "flow", "runs"),
        registered_path=("flow", "runs"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "runs"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_runs",
            include_options=True,
            workspace_requirement="flow_read",
            flow_read_action="runs",
        ),
        options_factory=_flow_runs_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        required_capabilities=("ticket_flow",),
    ),
    SlashCommandRoute(
        id="car.flow.issue",
        canonical_path=("car", "flow", "issue"),
        registered_path=("flow", "issue"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "issue"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_issue",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_issue_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow", "github_cli"),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.plan",
        canonical_path=("car", "flow", "plan"),
        registered_path=("flow", "plan"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "plan"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_plan",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_plan_options,
        ack_policy="immediate",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.start",
        canonical_path=("car", "flow", "start"),
        registered_path=("flow", "start"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "start"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_start",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_start_options,
        ack_policy="defer_public",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.restart",
        canonical_path=("car", "flow", "restart"),
        registered_path=("flow", "restart"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "restart"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_restart",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_run_picker_options,
        ack_policy="defer_public",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.resume",
        canonical_path=("car", "flow", "resume"),
        registered_path=("flow", "resume"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "resume"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_resume",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_run_picker_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.stop",
        canonical_path=("car", "flow", "stop"),
        registered_path=("flow", "stop"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "stop"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_stop",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_run_picker_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.archive",
        canonical_path=("car", "flow", "archive"),
        registered_path=("flow", "archive"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "archive"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_archive",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_run_picker_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.recover",
        canonical_path=("car", "flow", "recover"),
        registered_path=("flow", "recover"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "recover"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_recover",
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_run_picker_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
    SlashCommandRoute(
        id="car.flow.reply",
        canonical_path=("car", "flow", "reply"),
        registered_path=("flow", "reply"),
        description=next(
            spec.description for spec in FLOW_ACTION_SPECS if spec.name == "reply"
        ),
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_flow_reply",
            include_options=True,
            include_channel_id=True,
            include_guild_id=True,
            include_user_id=True,
            workspace_requirement="bound",
        ),
        options_factory=_flow_reply_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
        required_capabilities=("ticket_flow",),
        workspace_lock_policy="bound_workspace",
    ),
)

_SLASH_ROUTE_BY_CANONICAL_PATH = {
    route.canonical_path: route for route in _SLASH_ROUTES
}
_SLASH_ROUTE_BY_REGISTERED_PATH = {
    route.registered_path: route for route in _SLASH_ROUTES
}
_SLASH_ROUTES_BY_ID: dict[str, tuple[SlashCommandRoute, ...]] = {}
for _route in _SLASH_ROUTES:
    _SLASH_ROUTES_BY_ID.setdefault(_route.id, tuple())
    _SLASH_ROUTES_BY_ID[_route.id] = _SLASH_ROUTES_BY_ID[_route.id] + (_route,)


async def _handle_bind_page_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    page_token = custom_id.split(":", 1)[1].strip()
    await service._handle_bind_page_component(
        ctx.interaction_id,
        ctx.interaction_token,
        page_token=page_token,
    )


async def _handle_bind_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a repository and try again.",
        )
        return
    await service._handle_bind_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
        selected_workspace_value=ctx.values[0],
    )


async def _handle_flow_runs_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a run and try again.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_flow_status(
        ctx.interaction_id,
        ctx.interaction_token,
        workspace_root=workspace_root,
        options={"run_id": ctx.values[0]},
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
    )


async def _handle_agent_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an agent and try again.",
        )
        return
    await service._handle_car_agent(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"name": ctx.values[0]},
    )


async def _handle_agent_profile_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a Hermes profile and try again.",
        )
        return
    await service._handle_agent_profile_picker_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        selected_profile=ctx.values[0],
    )


async def _handle_model_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a model and try again.",
        )
        return
    await service._handle_model_picker_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        selected_model=ctx.values[0],
    )


async def _handle_model_effort_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select reasoning effort and try again.",
        )
        return
    await service._handle_model_effort_selection(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        selected_effort=ctx.values[0],
    )


async def _handle_session_resume_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a thread and try again.",
        )
        return
    await service._handle_car_resume(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"thread_id": ctx.values[0]},
    )


async def _handle_update_target_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an update target and try again.",
        )
        return
    await service._handle_car_update(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"target": ctx.values[0]},
        response_mode="component",
    )


async def _handle_update_confirm_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    raw_target = custom_id.split(":", 1)[1].strip()
    if not raw_target:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select an update target and try again.",
        )
        return
    await service._handle_car_update(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        options={"target": raw_target, "confirmed": True},
        response_mode="component",
    )


async def _handle_update_cancel_component(service: Any, ctx: Any) -> None:
    await service.update_component_message(
        interaction_id=ctx.interaction_id,
        interaction_token=ctx.interaction_token,
        text="Update cancelled.",
        components=[],
    )


def _parse_newt_component_custom_id(custom_id: str, prefix: str) -> Optional[str]:
    if custom_id == prefix:
        return ""
    if not custom_id.startswith(f"{prefix}:"):
        return None
    token = custom_id.split(":", 1)[1].strip()
    return token or None


async def _handle_newt_hard_reset_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    hard_reset_token = _parse_newt_component_custom_id(
        custom_id, NEWT_HARD_RESET_CUSTOM_ID
    )
    if hard_reset_token is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "I could not identify this interaction action. Please retry.",
        )
        return
    await service._handle_car_newt_hard_reset(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        expected_workspace_token=hard_reset_token,
    )


async def _handle_newt_cancel_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    cancel_token = _parse_newt_component_custom_id(custom_id, NEWT_CANCEL_CUSTOM_ID)
    if cancel_token is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "I could not identify this interaction action. Please retry.",
        )
        return
    await service._handle_car_newt_cancel(
        ctx.interaction_id,
        ctx.interaction_token,
        expected_workspace_token=cancel_token,
    )


async def _handle_review_commit_select_component(service: Any, ctx: Any) -> None:
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a commit and try again.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_car_review(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        workspace_root=workspace_root,
        options={"target": f"commit {ctx.values[0]}"},
    )


async def _handle_flow_action_select_component(service: Any, ctx: Any) -> None:
    custom_id = ctx.custom_id or ""
    action = custom_id.split(":", 1)[1].strip().lower()
    if not ctx.values:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Please select a run and try again.",
        )
        return
    if action not in FLOW_ACTIONS_WITH_RUN_PICKER:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            f"Unknown flow action picker: {action}",
        )
        return
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    run_id = ctx.values[0]
    handler_name = {
        "status": "_handle_flow_status",
        "restart": "_handle_flow_restart",
        "resume": "_handle_flow_resume",
        "stop": "_handle_flow_stop",
        "archive": "_handle_flow_archive",
        "recover": "_handle_flow_recover",
        "reply": "_handle_flow_reply",
    }.get(action)
    if handler_name is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            f"Unknown flow action picker: {action}",
        )
        return
    kwargs: dict[str, Any] = {
        "workspace_root": workspace_root,
        "options": {"run_id": run_id},
    }
    if action in {"status", "resume", "stop", "archive", "reply"}:
        kwargs["channel_id"] = ctx.channel_id
        kwargs["guild_id"] = ctx.guild_id
    if action == "reply":
        pending_key = service._pending_interaction_scope_key(
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
        )
        pending_text = service._pending_flow_reply_text.pop(pending_key, None)
        if not isinstance(pending_text, str) or not pending_text.strip():
            deferred = await ensure_ephemeral_response_deferred(
                service,
                ctx.interaction_id,
                ctx.interaction_token,
            )
            await service.send_or_respond_ephemeral(
                interaction_id=ctx.interaction_id,
                interaction_token=ctx.interaction_token,
                deferred=deferred,
                text="Reply selection expired. Re-run `/flow reply text:<...>`.",
            )
            return
        kwargs["options"] = {"run_id": run_id, "text": pending_text}
        kwargs["user_id"] = ctx.user_id
    await getattr(service, handler_name)(
        ctx.interaction_id,
        ctx.interaction_token,
        **kwargs,
    )


async def _prepare_flow_button_component(service: Any, ctx: Any) -> bool:
    custom_id = ctx.custom_id or ""
    flow_parts = custom_id.split(":")
    flow_action = flow_parts[2].strip().lower() if len(flow_parts) >= 3 else ""
    if flow_action not in FLOW_COMPONENT_PREPARE_ACTIONS:
        return True
    prepared = await ensure_component_response_deferred(
        service,
        ctx.interaction_id,
        ctx.interaction_token,
    )
    if prepared:
        return True
    await service.respond_ephemeral(
        ctx.interaction_id,
        ctx.interaction_token,
        "Discord interaction did not acknowledge. Please retry.",
    )
    return False


async def _handle_flow_button_component(service: Any, ctx: Any) -> None:
    workspace_root = await service._require_bound_workspace(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
    )
    if workspace_root is None:
        return
    await service._handle_flow_button(
        ctx.interaction_id,
        ctx.interaction_token,
        workspace_root=workspace_root,
        custom_id=ctx.custom_id or "",
        channel_id=ctx.channel_id,
        guild_id=ctx.guild_id,
    )


async def _handle_approval_component(service: Any, ctx: Any) -> None:
    await service._handle_approval_component(
        ctx.interaction_id,
        ctx.interaction_token,
        custom_id=ctx.custom_id or "",
    )


async def _handle_queue_cancel_component(service: Any, ctx: Any) -> None:
    await service._handle_queue_cancel_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        message_id=ctx.message_id,
        guild_id=ctx.guild_id,
    )


async def _handle_queue_interrupt_send_component(service: Any, ctx: Any) -> None:
    await service._handle_queue_interrupt_send_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        custom_id=ctx.custom_id or "",
        message_id=ctx.message_id,
        guild_id=ctx.guild_id,
        user_id=ctx.user_id,
    )


async def _handle_cancel_turn_component(service: Any, ctx: Any) -> None:
    await service._handle_cancel_turn_button(
        ctx.interaction_id,
        ctx.interaction_token,
        channel_id=ctx.channel_id,
        user_id=ctx.user_id,
        message_id=ctx.message_id,
        custom_id=ctx.custom_id or "",
    )


async def _handle_continue_turn_component(service: Any, ctx: Any) -> None:
    await service._handle_continue_turn_button(
        ctx.interaction_id,
        ctx.interaction_token,
    )


_COMPONENT_ROUTES: tuple[ComponentRoute, ...] = (
    ComponentRoute(
        id="tickets.filter",
        exact_custom_id=TICKETS_FILTER_SELECT_ID,
        handler=lambda service, ctx: service._handle_ticket_filter_component(
            ctx.interaction_id,
            ctx.interaction_token,
            channel_id=ctx.channel_id,
            values=ctx.values,
        ),
    ),
    ComponentRoute(
        id="tickets.select",
        exact_custom_id=TICKETS_SELECT_ID,
        handler=lambda service, ctx: service._handle_ticket_select_component(
            ctx.interaction_id,
            ctx.interaction_token,
            channel_id=ctx.channel_id,
            values=ctx.values,
        ),
    ),
    ComponentRoute(
        id="bind.page",
        custom_id_prefix=f"{BIND_PAGE_CUSTOM_ID_PREFIX}:",
        handler=_handle_bind_page_component,
    ),
    ComponentRoute(
        id="bind.select",
        exact_custom_id=BIND_SELECT_CUSTOM_ID,
        handler=_handle_bind_select_component,
        workspace_lock_policy="bind_target_workspace",
    ),
    ComponentRoute(
        id="flow.runs_select",
        exact_custom_id=FLOW_RUNS_SELECT_ID,
        handler=_handle_flow_runs_select_component,
    ),
    ComponentRoute(
        id="agent.select",
        exact_custom_id=AGENT_SELECT_CUSTOM_ID,
        handler=_handle_agent_select_component,
    ),
    ComponentRoute(
        id="agent.profile_select",
        exact_custom_id=AGENT_PROFILE_SELECT_ID,
        handler=_handle_agent_profile_select_component,
    ),
    ComponentRoute(
        id="model.select",
        exact_custom_id=MODEL_SELECT_CUSTOM_ID,
        handler=_handle_model_select_component,
    ),
    ComponentRoute(
        id="model.effort_select",
        exact_custom_id=MODEL_EFFORT_SELECT_ID,
        handler=_handle_model_effort_select_component,
    ),
    ComponentRoute(
        id="session.resume_select",
        exact_custom_id=SESSION_RESUME_SELECT_ID,
        handler=_handle_session_resume_select_component,
    ),
    ComponentRoute(
        id="update.target_select",
        exact_custom_id=UPDATE_TARGET_SELECT_ID,
        handler=_handle_update_target_select_component,
    ),
    ComponentRoute(
        id="update.confirm",
        custom_id_prefix=f"{UPDATE_CONFIRM_PREFIX}:",
        handler=_handle_update_confirm_component,
    ),
    ComponentRoute(
        id="update.cancel",
        custom_id_prefix=f"{UPDATE_CANCEL_PREFIX}:",
        handler=_handle_update_cancel_component,
    ),
    ComponentRoute(
        id="newt.hard_reset",
        custom_id_prefix=f"{NEWT_HARD_RESET_CUSTOM_ID}:",
        handler=_handle_newt_hard_reset_component,
        workspace_lock_policy="bound_workspace",
    ),
    ComponentRoute(
        id="newt.cancel",
        custom_id_prefix=f"{NEWT_CANCEL_CUSTOM_ID}:",
        handler=_handle_newt_cancel_component,
        workspace_lock_policy="bound_workspace",
    ),
    ComponentRoute(
        id="review.commit_select",
        exact_custom_id=REVIEW_COMMIT_SELECT_ID,
        handler=_handle_review_commit_select_component,
    ),
    ComponentRoute(
        id="flow.action_select",
        custom_id_prefix=f"{FLOW_ACTION_SELECT_PREFIX}:",
        handler=_handle_flow_action_select_component,
        workspace_lock_policy="bound_workspace",
    ),
    ComponentRoute(
        id="flow.button",
        custom_id_prefix="flow:",
        handler=_handle_flow_button_component,
        workspace_lock_policy="bound_workspace",
        prepare=_prepare_flow_button_component,
    ),
    ComponentRoute(
        id="approval.component",
        custom_id_prefix="approval:",
        handler=_handle_approval_component,
        workspace_lock_policy="bound_workspace",
    ),
    ComponentRoute(
        id="queue.cancel",
        custom_id_prefix="queue_cancel:",
        handler=_handle_queue_cancel_component,
    ),
    ComponentRoute(
        id="queue.interrupt_send",
        custom_id_prefix="queue_interrupt_send:",
        handler=_handle_queue_interrupt_send_component,
    ),
    ComponentRoute(
        id="turn.cancel",
        exact_custom_id="cancel_turn",
        handler=_handle_cancel_turn_component,
    ),
    ComponentRoute(
        id="turn.cancel_scoped",
        custom_id_prefix="cancel_turn:",
        handler=_handle_cancel_turn_component,
    ),
    ComponentRoute(
        id="turn.continue",
        exact_custom_id="continue_turn",
        handler=_handle_continue_turn_component,
    ),
)

_MODAL_ROUTES: tuple[ModalRoute, ...] = (
    ModalRoute(
        id="tickets.modal_submit",
        custom_id_prefix=f"{TICKETS_MODAL_PREFIX}:",
        handler=lambda service, ctx: service._handle_ticket_modal_submit(
            ctx.interaction_id,
            ctx.interaction_token,
            channel_id=ctx.channel_id,
            custom_id=ctx.custom_id or "",
            values=ctx.modal_values or {},
        ),
    ),
)

_AUTOCOMPLETE_ROUTES: tuple[AutocompleteRoute, ...] = (
    AutocompleteRoute(
        id="car.bind.workspace",
        command_path=("car", "bind"),
        focused_name="workspace",
        choices_builder=_build_bind_autocomplete_choices,
    ),
    AutocompleteRoute(
        id="car.model.name",
        command_path=("car", "model"),
        focused_name="name",
        choices_builder=_build_model_autocomplete_choices,
    ),
    AutocompleteRoute(
        id="car.skills.search",
        command_path=("car", "skills"),
        focused_name="search",
        choices_builder=_build_skills_autocomplete_choices,
    ),
    AutocompleteRoute(
        id="car.tickets.search",
        command_path=("car", "tickets"),
        focused_name="search",
        choices_builder=_build_ticket_autocomplete_choices,
    ),
    AutocompleteRoute(
        id="car.resume.thread_id",
        command_path=("car", "session", "resume"),
        focused_name="thread_id",
        choices_builder=_build_session_resume_autocomplete_choices,
    ),
)


def build_application_commands(context: Any = None) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    roots: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for route in _SLASH_ROUTES:
        if not route.include_in_payload:
            continue
        root_name = route.registered_path[0]
        root = roots.get(root_name)
        if root is None:
            root = {
                "type": 1,
                "name": root_name,
                "description": ROOT_COMMANDS[root_name],
                "options": [],
            }
            roots[root_name] = root
            commands.append(root)

        options = route.options_factory(context) if route.options_factory else None
        registered_path = route.registered_path
        if len(registered_path) == 2:
            option: dict[str, Any] = {
                "type": SUB_COMMAND,
                "name": registered_path[1],
                "description": route.description,
            }
            if options:
                option["options"] = options
            root["options"].append(option)
            continue

        group_key = (registered_path[0], registered_path[1])
        group = groups.get(group_key)
        if group is None:
            group = {
                "type": SUB_COMMAND_GROUP,
                "name": registered_path[1],
                "description": route.group_description or GROUP_DESCRIPTIONS[group_key],
                "options": [],
            }
            groups[group_key] = group
            root["options"].append(group)

        subcommand: dict[str, Any] = {
            "type": SUB_COMMAND,
            "name": registered_path[2],
            "description": route.description,
        }
        if options:
            subcommand["options"] = options
        group["options"].append(subcommand)

    return commands


def normalize_discord_command_path(command_path: tuple[str, ...]) -> tuple[str, ...]:
    route = _SLASH_ROUTE_BY_REGISTERED_PATH.get(command_path)
    if route is not None:
        return route.canonical_path
    return command_path


def slash_command_route_for_path(
    command_path: tuple[str, ...],
) -> Optional[SlashCommandRoute]:
    normalized_path = normalize_discord_command_path(command_path)
    return _SLASH_ROUTE_BY_CANONICAL_PATH.get(normalized_path)


def component_route_for_custom_id(custom_id: str) -> Optional[ComponentRoute]:
    for route in _COMPONENT_ROUTES:
        if route.matches(custom_id):
            return route
    return None


def modal_route_for_custom_id(custom_id: str) -> Optional[ModalRoute]:
    for route in _MODAL_ROUTES:
        if route.matches(custom_id):
            return route
    return None


def autocomplete_route_for(
    command_path: tuple[str, ...],
    focused_name: Optional[str],
) -> Optional[AutocompleteRoute]:
    normalized_path = normalize_discord_command_path(command_path)
    for route in _AUTOCOMPLETE_ROUTES:
        if route.matches(command_path=normalized_path, focused_name=focused_name):
            return route
    if (
        len(normalized_path) == 3
        and normalized_path[:2] == ("car", "flow")
        and normalized_path[2] in FLOW_ACTIONS_WITH_RUN_PICKER
        and focused_name == "run_id"
    ):
        return AutocompleteRoute(
            id=f"car.flow.{normalized_path[2]}.run_id",
            command_path=normalized_path,
            focused_name="run_id",
            choices_builder=_build_flow_run_autocomplete_choices,
        )
    return None


def slash_command_ack_metadata_for_path(
    command_path: tuple[str, ...],
) -> tuple[Optional[DiscordAckPolicy], DiscordAckTiming, bool]:
    route = slash_command_route_for_path(command_path)
    if route is None:
        return None, "dispatch", False
    return route.ack_policy, route.ack_timing, route.requires_workspace


def slash_command_workspace_lock_policy(
    command_path: tuple[str, ...],
) -> WorkspaceLockPolicy:
    route = slash_command_route_for_path(command_path)
    return route.workspace_lock_policy if route is not None else "none"


def component_scheduler_ack_strategy(custom_id: str) -> SchedulerAckStrategy:
    route = component_route_for_custom_id(custom_id)
    return (
        route.scheduler_ack_strategy
        if route is not None
        else "scheduler_component_update"
    )


def component_admission_ack_policy(custom_id: str) -> Optional[DiscordAckPolicy]:
    strategy = component_scheduler_ack_strategy(custom_id)
    if strategy == "scheduler_component_update":
        return "defer_component_update"
    if strategy == "scheduler_ephemeral":
        return "defer_ephemeral"
    return None


def component_workspace_lock_policy(custom_id: str) -> WorkspaceLockPolicy:
    route = component_route_for_custom_id(custom_id)
    return route.workspace_lock_policy if route is not None else "none"


def modal_scheduler_ack_strategy(custom_id: str) -> SchedulerAckStrategy:
    route = modal_route_for_custom_id(custom_id)
    return route.scheduler_ack_strategy if route is not None else "scheduler_ephemeral"


def modal_admission_ack_policy(custom_id: str) -> Optional[DiscordAckPolicy]:
    strategy = modal_scheduler_ack_strategy(custom_id)
    if strategy == "scheduler_component_update":
        return "defer_component_update"
    if strategy == "scheduler_ephemeral":
        return "defer_ephemeral"
    return None


def modal_workspace_lock_policy(custom_id: str) -> WorkspaceLockPolicy:
    route = modal_route_for_custom_id(custom_id)
    return route.workspace_lock_policy if route is not None else "bound_workspace"


def discord_contract_metadata_for_id(command_id: str) -> dict[str, Any]:
    routes = _SLASH_ROUTES_BY_ID.get(command_id, ())
    visible_routes = tuple(route for route in routes if route.catalog_in_contract)
    discord_paths = tuple(route.registered_path for route in visible_routes)
    if not visible_routes:
        required_capabilities: tuple[str, ...] = ()
        if routes:
            required_capabilities = routes[0].required_capabilities
        return {
            "discord_paths": (),
            "discord_ack_policy": None,
            "discord_ack_timing": "dispatch",
            "discord_exposure": None,
            "required_capabilities": required_capabilities,
        }
    route = visible_routes[0]
    return {
        "discord_paths": discord_paths,
        "discord_ack_policy": route.ack_policy,
        "discord_ack_timing": route.ack_timing,
        "discord_exposure": route.exposure,
        "required_capabilities": route.required_capabilities,
    }


async def dispatch_slash_command(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    command_path: tuple[str, ...],
    options: dict[str, Any],
) -> bool:
    route = slash_command_route_for_path(command_path)
    if route is None:
        if command_path[:1] == ("pma",):
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown PMA subcommand. Use on, off, or status.",
            )
            return False
        if command_path[:1] == ("car",):
            primary = command_path[1] if len(command_path) > 1 else ""
            await service.respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown car subcommand: {primary}",
            )
            return False
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Command not implemented yet for Discord.",
        )
        return False

    await route.handler(
        service,
        route,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id=user_id,
        options=options,
    )
    return True


async def dispatch_component_interaction(service: Any, ctx: Any) -> bool:
    custom_id = ctx.custom_id or ""
    route = component_route_for_custom_id(custom_id)
    if route is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            f"Unknown component: {custom_id}",
        )
        return False
    if route.prepare is not None:
        prepared_result = route.prepare(service, ctx)
        if inspect.isawaitable(prepared_result):
            prepared = await prepared_result
        else:
            prepared = prepared_result
        if not prepared:
            return False
    await route.handler(service, ctx)
    return True


async def dispatch_modal_submit(service: Any, ctx: Any) -> bool:
    route = modal_route_for_custom_id(ctx.custom_id or "")
    if route is None:
        await service.respond_ephemeral(
            ctx.interaction_id,
            ctx.interaction_token,
            "Unknown modal submission.",
        )
        return False
    await route.handler(service, ctx)
    return True


async def dispatch_autocomplete(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    command_path: tuple[str, ...],
    options: dict[str, Any],
    focused_name: Optional[str],
    focused_value: str,
) -> bool:
    route = autocomplete_route_for(command_path, focused_name)
    if route is None:
        await service.respond_autocomplete(
            interaction_id,
            interaction_token,
            choices=[],
        )
        return False
    choices_result = route.choices_builder(
        service,
        channel_id=channel_id,
        command_path=normalize_discord_command_path(command_path),
        options=options,
        focused_value=focused_value,
    )
    if inspect.isawaitable(choices_result):
        choices = await choices_result
    else:
        choices = choices_result
    await service.respond_autocomplete(
        interaction_id,
        interaction_token,
        choices=choices,
    )
    return True
