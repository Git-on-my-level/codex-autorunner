"""Discord runtime routing registry.

This module remains the single source of truth for slash-command, component,
modal, and autocomplete routing metadata. Handler shims and option builders
live in the extracted family modules:

- ``interaction_slash_builders``: slash option factories, handler shims,
  autocomplete choice builders, and ``_dispatch_service_method``.
- ``interaction_component_handlers``: component interaction handler shims.

This file owns the route dataclasses, contract resolution, route tables,
lookup/dispatch functions, and the application command builder.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from ...core.flows.catalog import FLOW_ACTION_SPECS, FLOW_ACTIONS_WITH_RUN_PICKER
from ..chat.action_ux_contract import (
    DiscordAckPolicy,
    DiscordAckTiming,
    DiscordExposure,
    SchedulerAckStrategy,
    discord_ack_policy_for_entry,
    discord_autocomplete_ux_contract_for_route,
    discord_component_ux_contract_for_route,
    discord_exposure_for_entry,
    discord_modal_ux_contract_for_route,
    discord_scheduler_ack_strategy_for_entry,
    discord_slash_command_ux_contract_for_id,
)
from .document_browser_component_handlers import (
    _handle_contextspace_back_component,
    _handle_contextspace_chunk_component,
    _handle_contextspace_page_component,
    _handle_contextspace_select_component,
    _handle_tickets_back_component,
    _handle_tickets_chunk_component,
    _handle_tickets_page_component,
)
from .interaction_component_handlers import (
    AGENT_PROFILE_SELECT_ID as AGENT_PROFILE_SELECT_ID,
)
from .interaction_component_handlers import (
    AGENT_SELECT_CUSTOM_ID as AGENT_SELECT_CUSTOM_ID,
)
from .interaction_component_handlers import (
    BIND_PAGE_CUSTOM_ID_PREFIX as BIND_PAGE_CUSTOM_ID_PREFIX,
)
from .interaction_component_handlers import (
    BIND_SELECT_CUSTOM_ID as BIND_SELECT_CUSTOM_ID,
)
from .interaction_component_handlers import (
    CONTEXTSPACE_BACK_CUSTOM_ID as CONTEXTSPACE_BACK_CUSTOM_ID,
)
from .interaction_component_handlers import (
    CONTEXTSPACE_CHUNK_CUSTOM_ID_PREFIX as CONTEXTSPACE_CHUNK_CUSTOM_ID_PREFIX,
)
from .interaction_component_handlers import (
    CONTEXTSPACE_PAGE_CUSTOM_ID_PREFIX as CONTEXTSPACE_PAGE_CUSTOM_ID_PREFIX,
)
from .interaction_component_handlers import (
    CONTEXTSPACE_SELECT_ID as CONTEXTSPACE_SELECT_ID,
)
from .interaction_component_handlers import (
    FLOW_ACTION_SELECT_PREFIX as FLOW_ACTION_SELECT_PREFIX,
)
from .interaction_component_handlers import (
    FLOW_RUNS_SELECT_ID as FLOW_RUNS_SELECT_ID,
)
from .interaction_component_handlers import (
    MODEL_EFFORT_SELECT_ID as MODEL_EFFORT_SELECT_ID,
)
from .interaction_component_handlers import (
    MODEL_SELECT_CUSTOM_ID as MODEL_SELECT_CUSTOM_ID,
)
from .interaction_component_handlers import (
    NEWT_CANCEL_CUSTOM_ID as NEWT_CANCEL_CUSTOM_ID,
)
from .interaction_component_handlers import (
    NEWT_HARD_RESET_CUSTOM_ID as NEWT_HARD_RESET_CUSTOM_ID,
)
from .interaction_component_handlers import (
    REVIEW_COMMIT_SELECT_ID as REVIEW_COMMIT_SELECT_ID,
)
from .interaction_component_handlers import (
    SESSION_RESUME_SELECT_ID as SESSION_RESUME_SELECT_ID,
)
from .interaction_component_handlers import (
    TICKETS_BACK_CUSTOM_ID as TICKETS_BACK_CUSTOM_ID,
)
from .interaction_component_handlers import (
    TICKETS_CHUNK_CUSTOM_ID_PREFIX as TICKETS_CHUNK_CUSTOM_ID_PREFIX,
)
from .interaction_component_handlers import (
    TICKETS_MODAL_PREFIX as TICKETS_MODAL_PREFIX,
)
from .interaction_component_handlers import (
    TICKETS_PAGE_CUSTOM_ID_PREFIX as TICKETS_PAGE_CUSTOM_ID_PREFIX,
)
from .interaction_component_handlers import (
    TICKETS_SELECT_ID as TICKETS_SELECT_ID,
)
from .interaction_component_handlers import (
    UPDATE_CANCEL_PREFIX as UPDATE_CANCEL_PREFIX,
)
from .interaction_component_handlers import (
    UPDATE_CONFIRM_PREFIX as UPDATE_CONFIRM_PREFIX,
)
from .interaction_component_handlers import (
    UPDATE_TARGET_SELECT_ID as UPDATE_TARGET_SELECT_ID,
)
from .interaction_component_handlers import (
    _handle_agent_profile_select_component,
    _handle_agent_select_component,
    _handle_approval_component,
    _handle_bind_page_component,
    _handle_bind_select_component,
    _handle_cancel_queued_turn_component,
    _handle_cancel_turn_component,
    _handle_continue_turn_component,
    _handle_flow_action_select_component,
    _handle_flow_button_component,
    _handle_flow_runs_select_component,
    _handle_model_effort_select_component,
    _handle_model_select_component,
    _handle_newt_cancel_component,
    _handle_newt_hard_reset_component,
    _handle_queue_cancel_component,
    _handle_queue_interrupt_send_component,
    _handle_queued_turn_interrupt_send_component,
    _handle_review_commit_select_component,
    _handle_session_resume_select_component,
    _handle_update_cancel_component,
    _handle_update_confirm_component,
    _handle_update_target_select_component,
)
from .interaction_slash_builders import (
    BOOLEAN as BOOLEAN,
)
from .interaction_slash_builders import (
    GROUP_DESCRIPTIONS as GROUP_DESCRIPTIONS,
)
from .interaction_slash_builders import (
    INTEGER as INTEGER,
)
from .interaction_slash_builders import (
    ROOT_COMMANDS as ROOT_COMMANDS,
)
from .interaction_slash_builders import (
    STRING as STRING,
)
from .interaction_slash_builders import (
    SUB_COMMAND as SUB_COMMAND,
)
from .interaction_slash_builders import (
    SUB_COMMAND_GROUP as SUB_COMMAND_GROUP,
)
from .interaction_slash_builders import (
    _agent_options,
    _approvals_options,
    _bind_options,
    _build_bind_autocomplete_choices,
    _build_flow_run_autocomplete_choices,
    _build_model_autocomplete_choices,
    _build_session_resume_autocomplete_choices,
    _build_skills_autocomplete_choices,
    _build_ticket_autocomplete_choices,
    _diff_options,
    _dispatch_service_method,
    _experimental_options,
    _feedback_options,
    _files_clear_options,
    _flow_issue_options,
    _flow_plan_options,
    _flow_reply_options,
    _flow_run_picker_options,
    _flow_runs_options,
    _flow_start_options,
    _flow_status_options,
    _handle_bind_slash,
    _handle_pma_route,
    _interrupt_extra_kwargs,
    _mention_options,
    _model_options,
    _review_options,
    _session_resume_options,
    _skills_options,
    _tickets_options,
    _update_options,
)

WorkspaceLockPolicy = Literal[
    "none",
    "bound_workspace",
    "bind_target_workspace",
]

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

    def __post_init__(self) -> None:
        if not self.catalog_in_contract:
            return
        contract_kwargs = _slash_contract_kwargs(self.id)
        object.__setattr__(self, "ack_policy", contract_kwargs["ack_policy"])
        object.__setattr__(self, "ack_timing", contract_kwargs["ack_timing"])
        object.__setattr__(self, "exposure", contract_kwargs["exposure"])


@dataclass(frozen=True)
class ComponentRoute:
    id: str
    handler: ComponentHandler
    exact_custom_id: Optional[str] = None
    custom_id_prefix: Optional[str] = None
    scheduler_ack_strategy: SchedulerAckStrategy = "scheduler_component_update"
    workspace_lock_policy: WorkspaceLockPolicy = "none"
    prepare: Optional[ComponentPrepare] = None
    contract_custom_ids: tuple[str, ...] = ()
    catalog_in_contract: bool = True

    def __post_init__(self) -> None:
        if not self.catalog_in_contract:
            return
        contract_kwargs = _component_contract_kwargs(
            self.id,
            exact_custom_id=self.exact_custom_id,
            custom_id_prefix=self.custom_id_prefix,
            contract_custom_ids=self.contract_custom_ids,
        )
        object.__setattr__(
            self,
            "scheduler_ack_strategy",
            contract_kwargs["scheduler_ack_strategy"],
        )
        object.__setattr__(
            self,
            "contract_custom_ids",
            contract_kwargs["contract_custom_ids"],
        )

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
    contract_custom_id: Optional[str] = None
    catalog_in_contract: bool = True

    def __post_init__(self) -> None:
        if not self.catalog_in_contract:
            return
        contract_kwargs = _modal_contract_kwargs(
            self.id,
            exact_custom_id=self.exact_custom_id,
            custom_id_prefix=self.custom_id_prefix,
            contract_custom_id=self.contract_custom_id,
        )
        object.__setattr__(
            self,
            "scheduler_ack_strategy",
            contract_kwargs["scheduler_ack_strategy"],
        )
        object.__setattr__(
            self,
            "contract_custom_id",
            contract_kwargs["contract_custom_id"],
        )

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
    catalog_in_contract: bool = True

    def __post_init__(self) -> None:
        if not self.catalog_in_contract:
            return
        ux_entry = discord_autocomplete_ux_contract_for_route(
            self.id,
            command_path=self.command_path,
            focused_name=self.focused_name,
        )
        if ux_entry is None:
            raise ValueError(
                f"missing shared Discord autocomplete UX contract for {self.id}"
            )

    def matches(
        self,
        *,
        command_path: tuple[str, ...],
        focused_name: Optional[str],
    ) -> bool:
        return command_path == self.command_path and focused_name == self.focused_name


def _default_contract_custom_ids(
    *,
    exact_custom_id: Optional[str],
    custom_id_prefix: Optional[str],
) -> tuple[str, ...]:
    if exact_custom_id is not None:
        return (exact_custom_id,)
    if custom_id_prefix is not None:
        return (f"{custom_id_prefix}sample",)
    return ()


def _slash_contract_kwargs(command_id: str) -> dict[str, Any]:
    ux_entry = discord_slash_command_ux_contract_for_id(command_id)
    if ux_entry is None:
        raise ValueError(f"missing shared Discord slash UX contract for {command_id}")
    ack_policy = discord_ack_policy_for_entry(ux_entry)
    exposure = discord_exposure_for_entry(ux_entry)
    if ack_policy is None:
        raise ValueError(
            f"shared Discord slash UX contract missing ack policy for {command_id}"
        )
    if exposure is None:
        raise ValueError(
            f"shared Discord slash UX contract missing exposure for {command_id}"
        )
    return {
        "ack_policy": ack_policy,
        "ack_timing": ux_entry.ack_timing,
        "exposure": exposure,
    }


def _component_contract_kwargs(
    route_id: str,
    *,
    exact_custom_id: Optional[str],
    custom_id_prefix: Optional[str],
    contract_custom_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    resolved_custom_ids = contract_custom_ids or _default_contract_custom_ids(
        exact_custom_id=exact_custom_id,
        custom_id_prefix=custom_id_prefix,
    )
    if not resolved_custom_ids:
        raise ValueError(f"missing contract custom id sample for {route_id}")
    ux_entry = discord_component_ux_contract_for_route(
        route_id,
        custom_id=resolved_custom_ids[0],
    )
    if ux_entry is None:
        raise ValueError(f"missing shared Discord component UX contract for {route_id}")
    for custom_id in resolved_custom_ids[1:]:
        if (
            discord_component_ux_contract_for_route(route_id, custom_id=custom_id)
            is None
        ):
            raise ValueError(
                f"missing shared Discord component UX contract for {route_id} via {custom_id}"
            )
    strategy = discord_scheduler_ack_strategy_for_entry(ux_entry)
    return {
        "scheduler_ack_strategy": (
            strategy if strategy != "none" else "scheduler_component_update"
        ),
        "contract_custom_ids": resolved_custom_ids,
    }


def _modal_contract_kwargs(
    route_id: str,
    *,
    exact_custom_id: Optional[str],
    custom_id_prefix: Optional[str],
    contract_custom_id: Optional[str] = None,
) -> dict[str, Any]:
    resolved_custom_id = contract_custom_id
    if resolved_custom_id is None:
        fallback_ids = _default_contract_custom_ids(
            exact_custom_id=exact_custom_id,
            custom_id_prefix=custom_id_prefix,
        )
        resolved_custom_id = fallback_ids[0] if fallback_ids else None
    if resolved_custom_id is None:
        raise ValueError(f"missing contract custom id sample for modal {route_id}")
    ux_entry = discord_modal_ux_contract_for_route(route_id)
    if ux_entry is None:
        raise ValueError(f"missing shared Discord modal UX contract for {route_id}")
    strategy = discord_scheduler_ack_strategy_for_entry(ux_entry)
    return {
        "scheduler_ack_strategy": (
            strategy if strategy != "none" else "scheduler_ephemeral"
        ),
        "contract_custom_id": resolved_custom_id,
    }


def _component_route(**kwargs: Any) -> ComponentRoute:
    route_id = str(kwargs["id"])
    exact_custom_id = kwargs.get("exact_custom_id")
    custom_id_prefix = kwargs.get("custom_id_prefix")
    contract_custom_ids = tuple(kwargs.pop("contract_custom_ids", ()))
    return ComponentRoute(
        **kwargs,
        **_component_contract_kwargs(
            route_id,
            exact_custom_id=exact_custom_id,
            custom_id_prefix=custom_id_prefix,
            contract_custom_ids=contract_custom_ids,
        ),
    )


def _modal_route(**kwargs: Any) -> ModalRoute:
    route_id = str(kwargs["id"])
    exact_custom_id = kwargs.get("exact_custom_id")
    custom_id_prefix = kwargs.get("custom_id_prefix")
    contract_custom_id = kwargs.pop("contract_custom_id", None)
    return ModalRoute(
        **kwargs,
        **_modal_contract_kwargs(
            route_id,
            exact_custom_id=exact_custom_id,
            custom_id_prefix=custom_id_prefix,
            contract_custom_id=contract_custom_id,
        ),
    )


def _autocomplete_route(**kwargs: Any) -> AutocompleteRoute:
    route = AutocompleteRoute(**kwargs)
    ux_entry = discord_autocomplete_ux_contract_for_route(
        route.id,
        command_path=route.command_path,
        focused_name=route.focused_name,
    )
    if ux_entry is None:
        raise ValueError(
            f"missing shared Discord autocomplete UX contract for {route.id}"
        )
    return route


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
        id="car.processes",
        canonical_path=("car", "processes"),
        registered_path=("car", "processes"),
        description="Show process monitor summary",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_processes",
            include_channel_id=True,
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
        description="Browse tickets",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_tickets",
            include_channel_id=True,
            include_user_id=True,
            include_options=True,
            workspace_requirement="bound",
        ),
        options_factory=_tickets_options,
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
    ),
    SlashCommandRoute(
        id="car.contextspace",
        canonical_path=("car", "contextspace"),
        registered_path=("car", "contextspace"),
        description="Browse contextspace docs",
        handler=lambda *args, **kwargs: _dispatch_service_method(
            *args,
            **kwargs,
            method_name="_handle_contextspace",
            include_channel_id=True,
            include_user_id=True,
            workspace_requirement="bound",
        ),
        ack_policy="defer_ephemeral",
        exposure="public",
        requires_workspace=True,
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


def _interrupt_component_route(
    *,
    id: str,
    handler: ComponentHandler,
    exact_custom_id: Optional[str] = None,
    custom_id_prefix: Optional[str] = None,
) -> ComponentRoute:
    return _component_route(
        id=id,
        exact_custom_id=exact_custom_id,
        custom_id_prefix=custom_id_prefix,
        handler=handler,
    )


_COMPONENT_ROUTES: tuple[ComponentRoute, ...] = (
    ComponentRoute(
        id="tickets.select",
        exact_custom_id=TICKETS_SELECT_ID,
        handler=lambda service, ctx: service._handle_ticket_select_component(
            ctx.interaction_id,
            ctx.interaction_token,
            channel_id=ctx.channel_id,
            user_id=ctx.user_id,
            values=ctx.values,
        ),
    ),
    ComponentRoute(
        id="tickets.page",
        custom_id_prefix=f"{TICKETS_PAGE_CUSTOM_ID_PREFIX}:",
        handler=_handle_tickets_page_component,
    ),
    ComponentRoute(
        id="tickets.back",
        exact_custom_id=TICKETS_BACK_CUSTOM_ID,
        handler=_handle_tickets_back_component,
    ),
    ComponentRoute(
        id="tickets.chunk",
        custom_id_prefix=f"{TICKETS_CHUNK_CUSTOM_ID_PREFIX}:",
        handler=_handle_tickets_chunk_component,
    ),
    ComponentRoute(
        id="contextspace.select",
        exact_custom_id=CONTEXTSPACE_SELECT_ID,
        handler=_handle_contextspace_select_component,
    ),
    ComponentRoute(
        id="contextspace.page",
        custom_id_prefix=f"{CONTEXTSPACE_PAGE_CUSTOM_ID_PREFIX}:",
        handler=_handle_contextspace_page_component,
    ),
    ComponentRoute(
        id="contextspace.back",
        exact_custom_id=CONTEXTSPACE_BACK_CUSTOM_ID,
        handler=_handle_contextspace_back_component,
    ),
    ComponentRoute(
        id="contextspace.chunk",
        custom_id_prefix=f"{CONTEXTSPACE_CHUNK_CUSTOM_ID_PREFIX}:",
        handler=_handle_contextspace_chunk_component,
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
        contract_custom_ids=("flow:run-1:stop", "flow:run-1:refresh"),
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
        id="queued_turn.cancel",
        custom_id_prefix="qcancel:",
        handler=_handle_cancel_queued_turn_component,
    ),
    _interrupt_component_route(
        id="queue.interrupt_send",
        custom_id_prefix="queue_interrupt_send:",
        handler=_handle_queue_interrupt_send_component,
    ),
    _interrupt_component_route(
        id="queued_turn.interrupt_send",
        custom_id_prefix="qis:",
        handler=_handle_queued_turn_interrupt_send_component,
    ),
    _interrupt_component_route(
        id="turn.cancel",
        exact_custom_id="cancel_turn",
        handler=_handle_cancel_turn_component,
    ),
    _interrupt_component_route(
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
    _modal_route(
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
    _autocomplete_route(
        id="car.bind.workspace",
        command_path=("car", "bind"),
        focused_name="workspace",
        choices_builder=_build_bind_autocomplete_choices,
    ),
    _autocomplete_route(
        id="car.model.name",
        command_path=("car", "model"),
        focused_name="name",
        choices_builder=_build_model_autocomplete_choices,
    ),
    _autocomplete_route(
        id="car.skills.search",
        command_path=("car", "skills"),
        focused_name="search",
        choices_builder=_build_skills_autocomplete_choices,
    ),
    _autocomplete_route(
        id="car.tickets.search",
        command_path=("car", "tickets"),
        focused_name="search",
        choices_builder=_build_ticket_autocomplete_choices,
    ),
    _autocomplete_route(
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


def cataloged_component_contract_scenarios() -> tuple[tuple[str, str], ...]:
    scenarios: list[tuple[str, str]] = []
    for route in _COMPONENT_ROUTES:
        if not route.catalog_in_contract:
            continue
        for custom_id in route.contract_custom_ids:
            scenarios.append((route.id, custom_id))
    return tuple(scenarios)


def cataloged_modal_contract_scenarios() -> tuple[tuple[str, str], ...]:
    scenarios: list[tuple[str, str]] = []
    for route in _MODAL_ROUTES:
        if not route.catalog_in_contract or route.contract_custom_id is None:
            continue
        scenarios.append((route.id, route.contract_custom_id))
    return tuple(scenarios)


def cataloged_autocomplete_contract_scenarios(
    *,
    include_dynamic_flow_run_picker: bool = True,
) -> tuple[tuple[Optional[str], tuple[str, ...], str], ...]:
    scenarios: list[tuple[Optional[str], tuple[str, ...], str]] = [
        (route.id, route.command_path, route.focused_name)
        for route in _AUTOCOMPLETE_ROUTES
        if route.catalog_in_contract
    ]
    if include_dynamic_flow_run_picker:
        scenarios.extend(
            (
                None,
                ("car", "flow", action),
                "run_id",
            )
            for action in sorted(FLOW_ACTIONS_WITH_RUN_PICKER)
        )
    return tuple(scenarios)


def slash_command_ack_metadata_for_path(
    command_path: tuple[str, ...],
) -> tuple[Optional[DiscordAckPolicy], DiscordAckTiming, bool]:
    route = slash_command_route_for_path(command_path)
    if route is None:
        return None, "dispatch", False
    ux_entry = discord_slash_command_ux_contract_for_id(route.id)
    ack_policy = discord_ack_policy_for_entry(ux_entry)
    ack_timing = ux_entry.ack_timing if ux_entry is not None else "dispatch"
    return ack_policy, ack_timing, route.requires_workspace


def slash_command_workspace_lock_policy(
    command_path: tuple[str, ...],
) -> WorkspaceLockPolicy:
    route = slash_command_route_for_path(command_path)
    return route.workspace_lock_policy if route is not None else "none"


def component_scheduler_ack_strategy(custom_id: str) -> SchedulerAckStrategy:
    route = component_route_for_custom_id(custom_id)
    if route is None:
        return "scheduler_component_update"
    ux_entry = discord_component_ux_contract_for_route(route.id, custom_id=custom_id)
    strategy = discord_scheduler_ack_strategy_for_entry(ux_entry)
    return strategy if strategy != "none" else route.scheduler_ack_strategy


def component_dispatch_ack_policy(custom_id: str) -> Optional[DiscordAckPolicy]:
    route = component_route_for_custom_id(custom_id)
    if route is None:
        return None
    ux_entry = discord_component_ux_contract_for_route(route.id, custom_id=custom_id)
    return discord_ack_policy_for_entry(ux_entry, dispatch=True)


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
    if route is None:
        return "scheduler_ephemeral"
    ux_entry = discord_modal_ux_contract_for_route(route.id)
    strategy = discord_scheduler_ack_strategy_for_entry(ux_entry)
    return strategy if strategy != "none" else route.scheduler_ack_strategy


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
    ux_entry = discord_slash_command_ux_contract_for_id(command_id)
    if not visible_routes:
        required_capabilities: tuple[str, ...] = ()
        if routes:
            required_capabilities = routes[0].required_capabilities
        return {
            "discord_paths": (),
            "discord_ack_policy": discord_ack_policy_for_entry(ux_entry),
            "discord_ack_timing": (
                ux_entry.ack_timing if ux_entry is not None else "dispatch"
            ),
            "discord_exposure": discord_exposure_for_entry(ux_entry),
            "required_capabilities": required_capabilities,
        }
    route = visible_routes[0]
    return {
        "discord_paths": discord_paths,
        "discord_ack_policy": discord_ack_policy_for_entry(ux_entry),
        "discord_ack_timing": (
            ux_entry.ack_timing if ux_entry is not None else route.ack_timing
        ),
        "discord_exposure": discord_exposure_for_entry(ux_entry),
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
    if discord_slash_command_ux_contract_for_id(route.id) is None:
        raise ValueError(f"missing shared Discord slash UX contract for {route.id}")

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
    if discord_component_ux_contract_for_route(route.id, custom_id=custom_id) is None:
        raise ValueError(f"missing shared Discord component UX contract for {route.id}")
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
    if discord_modal_ux_contract_for_route(route.id) is None:
        raise ValueError(f"missing shared Discord modal UX contract for {route.id}")
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
        if (
            discord_autocomplete_ux_contract_for_route(
                None,
                command_path=normalize_discord_command_path(command_path),
                focused_name=focused_name,
            )
            is None
        ):
            await service.respond_autocomplete(
                interaction_id,
                interaction_token,
                choices=[],
            )
            return False
        await service.respond_autocomplete(
            interaction_id,
            interaction_token,
            choices=[],
        )
        return False
    if (
        discord_autocomplete_ux_contract_for_route(
            route.id,
            command_path=normalize_discord_command_path(command_path),
            focused_name=focused_name,
        )
        is None
    ):
        raise ValueError(
            f"missing shared Discord autocomplete UX contract for {route.id}"
        )
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
