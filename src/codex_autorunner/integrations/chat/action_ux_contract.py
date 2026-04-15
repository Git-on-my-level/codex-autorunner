"""Shared chat action UX contract for commands, callbacks, and controls.

This module centralizes the question "how should this user action feel?" for
Telegram and Discord. Surface adapters may still own transport mechanics, but
user-visible acknowledgement, queue posture, first feedback, and control
affordances live here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Optional, cast

TelegramExposure = Literal["public", "hidden", "legacy_alias"]
TelegramResponsePolicy = Literal["typing"]
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
ChatActionSurface = Literal[
    "telegram_command",
    "discord_slash_command",
    "telegram_callback",
    "discord_component",
    "discord_modal",
    "discord_autocomplete",
    "plain_text_turn",
    "control",
]
ChatActionAckClass = Literal[
    "typing",
    "immediate",
    "defer_ephemeral",
    "defer_public",
    "defer_component_update",
    "callback_answer",
    "modal",
    "autocomplete",
    "none",
]
ChatActionQueuePolicy = Literal[
    "serialize",
    "scheduler_serialized",
    "allow_during_turn",
    "bypass_active_turn",
    "control_priority",
]
FirstVisibleFeedbackPolicy = Literal[
    "typing_indicator",
    "ephemeral_ack",
    "public_ack",
    "component_update",
    "callback_answer",
    "modal_open",
    "autocomplete_choices",
    "anchor_refresh",
    "none",
]
ChatActionPriority = Literal["normal", "control", "interrupt"]
AnchorMessageReuse = Literal["never", "prefer", "require"]
ChatActionVisibility = Literal["public", "operator", "hidden", "legacy_alias"]

CHAT_ACTION_UX_CONTRACT_VERSION = "chat-action-ux-v1"


@dataclass(frozen=True)
class ChatActionUxContractEntry:
    """Shared UX metadata for one surface action or canonical control."""

    id: str
    surface: ChatActionSurface
    lookup_keys: tuple[str, ...]
    ack_class: ChatActionAckClass
    queue_policy: ChatActionQueuePolicy
    first_visible_feedback: FirstVisibleFeedbackPolicy
    optimistic_ui_allowed: bool
    control_priority: ChatActionPriority
    anchor_message_reuse: AnchorMessageReuse
    visibility: ChatActionVisibility = "public"
    ack_timing: DiscordAckTiming = "dispatch"
    dispatch_ack_class: Optional[ChatActionAckClass] = None


def _first_visible_feedback_for_ack(
    ack_class: ChatActionAckClass,
) -> FirstVisibleFeedbackPolicy:
    if ack_class == "typing":
        return "typing_indicator"
    if ack_class == "defer_public":
        return "public_ack"
    if ack_class in {"immediate", "defer_ephemeral"}:
        return "ephemeral_ack"
    if ack_class == "defer_component_update":
        return "component_update"
    if ack_class == "callback_answer":
        return "callback_answer"
    if ack_class == "modal":
        return "modal_open"
    if ack_class == "autocomplete":
        return "autocomplete_choices"
    return "none"


def _telegram_command_entry(
    name: str,
    *,
    visibility: TelegramExposure = "public",
    allow_during_turn: bool,
) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"telegram_command.{name}",
        surface="telegram_command",
        lookup_keys=(f"telegram_command:{name}",),
        ack_class="typing",
        queue_policy="allow_during_turn" if allow_during_turn else "serialize",
        first_visible_feedback=_first_visible_feedback_for_ack("typing"),
        optimistic_ui_allowed=False,
        control_priority="interrupt" if name == "interrupt" else "normal",
        anchor_message_reuse=(
            "prefer" if name in {"flow", "status", "interrupt"} else "never"
        ),
        visibility=visibility,
    )


def _discord_slash_entry(
    command_id: str,
    *,
    ack_class: ChatActionAckClass,
    visibility: DiscordExposure,
    ack_timing: DiscordAckTiming = "dispatch",
    control_priority: ChatActionPriority = "normal",
    anchor_message_reuse: AnchorMessageReuse = "never",
) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"discord_slash_command.{command_id}",
        surface="discord_slash_command",
        lookup_keys=(f"discord_slash_command:{command_id}",),
        ack_class=ack_class,
        queue_policy=(
            "control_priority"
            if control_priority != "normal"
            else "scheduler_serialized"
        ),
        first_visible_feedback=_first_visible_feedback_for_ack(ack_class),
        optimistic_ui_allowed=False,
        control_priority=control_priority,
        anchor_message_reuse=anchor_message_reuse,
        visibility=visibility,
        ack_timing=ack_timing,
    )


def _telegram_callback_entry(
    callback_id: str,
    *,
    queue_policy: ChatActionQueuePolicy = "serialize",
    control_priority: ChatActionPriority = "normal",
    anchor_message_reuse: AnchorMessageReuse = "never",
    optimistic_ui_allowed: bool = True,
) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"telegram_callback.{callback_id}",
        surface="telegram_callback",
        lookup_keys=(f"telegram_callback:{callback_id}",),
        ack_class="callback_answer",
        queue_policy=queue_policy,
        first_visible_feedback=_first_visible_feedback_for_ack("callback_answer"),
        optimistic_ui_allowed=optimistic_ui_allowed,
        control_priority=control_priority,
        anchor_message_reuse=anchor_message_reuse,
    )


def _discord_component_entry(
    route_id: str,
    *,
    ack_class: ChatActionAckClass = "defer_component_update",
    dispatch_ack_class: Optional[ChatActionAckClass] = None,
    queue_policy: ChatActionQueuePolicy = "scheduler_serialized",
    control_priority: ChatActionPriority = "normal",
    anchor_message_reuse: AnchorMessageReuse = "prefer",
    optimistic_ui_allowed: bool = True,
) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"discord_component.{route_id}",
        surface="discord_component",
        lookup_keys=(f"discord_component:{route_id}",),
        ack_class=ack_class,
        queue_policy=queue_policy,
        first_visible_feedback=_first_visible_feedback_for_ack(ack_class),
        optimistic_ui_allowed=optimistic_ui_allowed,
        control_priority=control_priority,
        anchor_message_reuse=anchor_message_reuse,
        dispatch_ack_class=dispatch_ack_class,
    )


def _discord_modal_entry(route_id: str) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"discord_modal.{route_id}",
        surface="discord_modal",
        lookup_keys=(f"discord_modal:{route_id}",),
        ack_class="defer_ephemeral",
        queue_policy="scheduler_serialized",
        first_visible_feedback=_first_visible_feedback_for_ack("defer_ephemeral"),
        optimistic_ui_allowed=False,
        control_priority="normal",
        anchor_message_reuse="never",
    )


def _discord_autocomplete_entry(route_id: str) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"discord_autocomplete.{route_id}",
        surface="discord_autocomplete",
        lookup_keys=(f"discord_autocomplete:{route_id}",),
        ack_class="autocomplete",
        queue_policy="bypass_active_turn",
        first_visible_feedback=_first_visible_feedback_for_ack("autocomplete"),
        optimistic_ui_allowed=True,
        control_priority="normal",
        anchor_message_reuse="never",
    )


def _plain_text_turn_entry(mode: str) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"plain_text_turn.{mode}",
        surface="plain_text_turn",
        lookup_keys=(f"plain_text_turn:{mode}",),
        ack_class="none",
        queue_policy="scheduler_serialized",
        first_visible_feedback="anchor_refresh",
        optimistic_ui_allowed=False,
        control_priority="normal",
        anchor_message_reuse="prefer",
    )


def _control_entry(
    action_id: str,
    *,
    ack_class: ChatActionAckClass,
    dispatch_ack_class: Optional[ChatActionAckClass] = None,
    first_visible_feedback: Optional[FirstVisibleFeedbackPolicy] = None,
    control_priority: ChatActionPriority,
    anchor_message_reuse: AnchorMessageReuse,
    optimistic_ui_allowed: bool,
) -> ChatActionUxContractEntry:
    return ChatActionUxContractEntry(
        id=f"control.{action_id}",
        surface="control",
        lookup_keys=(f"control:{action_id}",),
        ack_class=ack_class,
        queue_policy="control_priority",
        first_visible_feedback=first_visible_feedback
        or _first_visible_feedback_for_ack(ack_class),
        optimistic_ui_allowed=optimistic_ui_allowed,
        control_priority=control_priority,
        anchor_message_reuse=anchor_message_reuse,
        dispatch_ack_class=dispatch_ack_class,
    )


CHAT_ACTION_UX_CONTRACT: tuple[ChatActionUxContractEntry, ...] = (
    # Telegram commands.
    _telegram_command_entry("repos", allow_during_turn=True),
    _telegram_command_entry("bind", allow_during_turn=False),
    _telegram_command_entry("new", allow_during_turn=False),
    _telegram_command_entry("newt", allow_during_turn=False),
    _telegram_command_entry("archive", allow_during_turn=False),
    _telegram_command_entry("reset", allow_during_turn=False),
    _telegram_command_entry("resume", allow_during_turn=False),
    _telegram_command_entry("review", allow_during_turn=False),
    _telegram_command_entry("flow", allow_during_turn=True),
    _telegram_command_entry(
        "reply",
        visibility="legacy_alias",
        allow_during_turn=True,
    ),
    _telegram_command_entry("agent", allow_during_turn=False),
    _telegram_command_entry("model", allow_during_turn=False),
    _telegram_command_entry("approvals", allow_during_turn=False),
    _telegram_command_entry("pma", allow_during_turn=True),
    _telegram_command_entry("status", allow_during_turn=True),
    _telegram_command_entry("processes", allow_during_turn=True),
    _telegram_command_entry("files", allow_during_turn=True),
    _telegram_command_entry("debug", allow_during_turn=True),
    _telegram_command_entry("ids", allow_during_turn=True),
    _telegram_command_entry("diff", allow_during_turn=True),
    _telegram_command_entry("mention", allow_during_turn=True),
    _telegram_command_entry("skills", allow_during_turn=True),
    _telegram_command_entry("mcp", visibility="hidden", allow_during_turn=True),
    _telegram_command_entry(
        "experimental",
        visibility="hidden",
        allow_during_turn=False,
    ),
    _telegram_command_entry("init", allow_during_turn=False),
    _telegram_command_entry("compact", allow_during_turn=False),
    _telegram_command_entry("rollout", allow_during_turn=True),
    _telegram_command_entry("update", allow_during_turn=False),
    _telegram_command_entry("logout", allow_during_turn=False),
    _telegram_command_entry("feedback", allow_during_turn=True),
    _telegram_command_entry("interrupt", allow_during_turn=True),
    _telegram_command_entry("help", allow_during_turn=True),
    # Discord slash commands.
    _discord_slash_entry("car.bind", ack_class="defer_ephemeral", visibility="public"),
    _discord_slash_entry(
        "car.status", ack_class="defer_ephemeral", visibility="public"
    ),
    _discord_slash_entry(
        "car.processes",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry("car.new", ack_class="defer_public", visibility="public"),
    _discord_slash_entry("car.newt", ack_class="defer_public", visibility="public"),
    _discord_slash_entry("car.agent", ack_class="immediate", visibility="public"),
    _discord_slash_entry("car.model", ack_class="defer_ephemeral", visibility="public"),
    _discord_slash_entry(
        "car.update", ack_class="defer_ephemeral", visibility="public"
    ),
    _discord_slash_entry("car.help", ack_class="immediate", visibility="operator"),
    _discord_slash_entry(
        "car.debug", ack_class="defer_ephemeral", visibility="operator"
    ),
    _discord_slash_entry("car.ids", ack_class="immediate", visibility="operator"),
    _discord_slash_entry("car.diff", ack_class="defer_ephemeral", visibility="public"),
    _discord_slash_entry(
        "car.skills", ack_class="defer_ephemeral", visibility="public"
    ),
    _discord_slash_entry(
        "car.tickets", ack_class="defer_ephemeral", visibility="public"
    ),
    _discord_slash_entry("car.mcp", ack_class="defer_ephemeral", visibility="operator"),
    _discord_slash_entry(
        "car.init", ack_class="defer_ephemeral", visibility="operator"
    ),
    _discord_slash_entry("car.repos", ack_class="immediate", visibility="operator"),
    _discord_slash_entry(
        "car.archive", ack_class="defer_ephemeral", visibility="public"
    ),
    _discord_slash_entry(
        "car.files.inbox",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.files.outbox",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.files.clear",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.status",
        ack_class="defer_public",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.runs",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.issue",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry("car.flow.plan", ack_class="immediate", visibility="public"),
    _discord_slash_entry(
        "car.flow.start",
        ack_class="defer_public",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.restart",
        ack_class="defer_public",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.resume",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.stop",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.archive",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.recover",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.flow.reply",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry("pma.on", ack_class="immediate", visibility="public"),
    _discord_slash_entry("pma.off", ack_class="immediate", visibility="public"),
    _discord_slash_entry("pma.status", ack_class="immediate", visibility="public"),
    _discord_slash_entry(
        "car.resume",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.reset",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.review",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.approvals",
        ack_class="immediate",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.mention",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.experimental",
        ack_class="immediate",
        visibility="operator",
    ),
    _discord_slash_entry(
        "car.compact",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry("car.rollout", ack_class="immediate", visibility="operator"),
    _discord_slash_entry(
        "car.logout",
        ack_class="defer_ephemeral",
        visibility="public",
    ),
    _discord_slash_entry(
        "car.feedback",
        ack_class="defer_ephemeral",
        visibility="operator",
    ),
    _discord_slash_entry(
        "car.interrupt",
        ack_class="defer_ephemeral",
        visibility="public",
        control_priority="interrupt",
        anchor_message_reuse="prefer",
    ),
    # Telegram callbacks.
    _telegram_callback_entry(
        "approval",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _telegram_callback_entry(
        "question_option",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _telegram_callback_entry(
        "question_done",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _telegram_callback_entry(
        "question_custom",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _telegram_callback_entry(
        "question_cancel",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _telegram_callback_entry("resume"),
    _telegram_callback_entry("bind"),
    _telegram_callback_entry("agent"),
    _telegram_callback_entry("agent_profile"),
    _telegram_callback_entry("model"),
    _telegram_callback_entry("effort"),
    _telegram_callback_entry("update"),
    _telegram_callback_entry("update_confirm"),
    _telegram_callback_entry("review_commit"),
    _telegram_callback_entry("compact", anchor_message_reuse="prefer"),
    _telegram_callback_entry("flow", anchor_message_reuse="prefer"),
    _telegram_callback_entry("flow_run"),
    _telegram_callback_entry(
        "cancel.selection",
        queue_policy="bypass_active_turn",
        control_priority="control",
        anchor_message_reuse="prefer",
    ),
    # Discord components.
    _discord_component_entry("tickets.filter"),
    _discord_component_entry("tickets.select"),
    _discord_component_entry("bind.select"),
    _discord_component_entry("flow.runs_select"),
    _discord_component_entry("agent.select"),
    _discord_component_entry("agent.profile_select"),
    _discord_component_entry("model.select"),
    _discord_component_entry("model.effort_select"),
    _discord_component_entry("session.resume_select"),
    _discord_component_entry("update.target_select"),
    _discord_component_entry("update.confirm"),
    _discord_component_entry("update.cancel"),
    _discord_component_entry("newt.hard_reset"),
    _discord_component_entry("newt.cancel"),
    _discord_component_entry("review.commit_select"),
    _discord_component_entry("flow.action_select"),
    _discord_component_entry(
        "flow.button",
        dispatch_ack_class="defer_component_update",
    ),
    _discord_component_entry(
        "approval.component",
        queue_policy="bypass_active_turn",
        control_priority="control",
    ),
    _discord_component_entry(
        "turn.continue",
        queue_policy="control_priority",
        control_priority="control",
        anchor_message_reuse="prefer",
    ),
    # Discord modals and autocomplete.
    _discord_modal_entry("tickets.modal_submit"),
    _discord_autocomplete_entry("car.bind.workspace"),
    _discord_autocomplete_entry("car.model.name"),
    _discord_autocomplete_entry("car.skills.search"),
    _discord_autocomplete_entry("car.tickets.search"),
    _discord_autocomplete_entry("car.resume.thread_id"),
    _discord_autocomplete_entry("flow.run_picker"),
    # Plain-text turns.
    _plain_text_turn_entry("always"),
    _plain_text_turn_entry("mentions"),
    # Shared controls reused across surfaces.
    _control_entry(
        "interrupt",
        ack_class="defer_ephemeral",
        control_priority="interrupt",
        anchor_message_reuse="prefer",
        optimistic_ui_allowed=True,
    ),
    _control_entry(
        "queue_cancel",
        ack_class="defer_component_update",
        first_visible_feedback="anchor_refresh",
        control_priority="control",
        anchor_message_reuse="require",
        optimistic_ui_allowed=True,
    ),
    _control_entry(
        "queue_interrupt_send",
        ack_class="defer_ephemeral",
        control_priority="interrupt",
        anchor_message_reuse="prefer",
        optimistic_ui_allowed=True,
    ),
    _control_entry(
        "refresh",
        ack_class="defer_component_update",
        dispatch_ack_class="defer_component_update",
        first_visible_feedback="anchor_refresh",
        control_priority="control",
        anchor_message_reuse="require",
        optimistic_ui_allowed=True,
    ),
    _control_entry(
        "pagination",
        ack_class="defer_component_update",
        first_visible_feedback="anchor_refresh",
        control_priority="control",
        anchor_message_reuse="require",
        optimistic_ui_allowed=True,
    ),
)


@lru_cache(maxsize=None)
def _contract_by_lookup_key(
    ux_contract: tuple[ChatActionUxContractEntry, ...],
) -> dict[str, ChatActionUxContractEntry]:
    lookup: dict[str, ChatActionUxContractEntry] = {}
    for entry in ux_contract:
        for key in entry.lookup_keys:
            lookup[key] = entry
    return lookup


def _normalize_contract(
    ux_contract: Sequence[ChatActionUxContractEntry],
) -> tuple[ChatActionUxContractEntry, ...]:
    if isinstance(ux_contract, tuple):
        return ux_contract
    return tuple(ux_contract)


def _lookup_entry(
    key: str,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized_contract = _normalize_contract(ux_contract)
    return _contract_by_lookup_key(normalized_contract).get(key)


def telegram_command_ux_contract_for_name(
    name: str,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized = str(name or "").strip()
    if not normalized:
        return None
    return _lookup_entry(
        f"telegram_command:{normalized}",
        ux_contract=ux_contract,
    )


def discord_slash_command_ux_contract_for_id(
    command_id: str,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized = str(command_id or "").strip()
    if not normalized:
        return None
    return _lookup_entry(
        f"discord_slash_command:{normalized}",
        ux_contract=ux_contract,
    )


def telegram_callback_ux_contract_for_callback(
    callback_id: str,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized = str(callback_id or "").strip().lower()
    fields = payload or {}
    if normalized == "cancel":
        kind = str(fields.get("kind") or "").strip().lower()
        if kind == "interrupt":
            return _lookup_entry("control:interrupt", ux_contract=ux_contract)
        if kind.startswith("queue_cancel:"):
            return _lookup_entry("control:queue_cancel", ux_contract=ux_contract)
        if kind.startswith("queue_interrupt_send:"):
            return _lookup_entry(
                "control:queue_interrupt_send",
                ux_contract=ux_contract,
            )
        return _lookup_entry(
            "telegram_callback:cancel.selection",
            ux_contract=ux_contract,
        )
    if normalized == "page":
        return _lookup_entry("control:pagination", ux_contract=ux_contract)
    if normalized == "flow":
        action = str(fields.get("action") or "").strip().lower()
        if action == "refresh":
            return _lookup_entry("control:refresh", ux_contract=ux_contract)
    return _lookup_entry(f"telegram_callback:{normalized}", ux_contract=ux_contract)


def discord_component_ux_contract_for_route(
    route_id: str,
    *,
    custom_id: str = "",
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized_route_id = str(route_id or "").strip()
    normalized_custom_id = str(custom_id or "").strip().lower()
    if normalized_route_id in {"turn.cancel", "turn.cancel_scoped"}:
        return _lookup_entry("control:interrupt", ux_contract=ux_contract)
    if normalized_route_id in {"queue.cancel", "queued_turn.cancel"}:
        return _lookup_entry("control:queue_cancel", ux_contract=ux_contract)
    if normalized_route_id in {
        "queue.interrupt_send",
        "queued_turn.interrupt_send",
    }:
        return _lookup_entry("control:queue_interrupt_send", ux_contract=ux_contract)
    if normalized_route_id == "bind.page":
        return _lookup_entry("control:pagination", ux_contract=ux_contract)
    if normalized_route_id == "flow.button":
        parts = normalized_custom_id.split(":")
        flow_action = parts[2].strip() if len(parts) >= 3 else ""
        if flow_action == "refresh":
            return _lookup_entry("control:refresh", ux_contract=ux_contract)
    return _lookup_entry(
        f"discord_component:{normalized_route_id}",
        ux_contract=ux_contract,
    )


def discord_modal_ux_contract_for_route(
    route_id: str,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized = str(route_id or "").strip()
    if not normalized:
        return None
    return _lookup_entry(f"discord_modal:{normalized}", ux_contract=ux_contract)


def discord_autocomplete_ux_contract_for_route(
    route_id: Optional[str],
    *,
    command_path: tuple[str, ...] = (),
    focused_name: Optional[str] = None,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized_route_id = str(route_id or "").strip()
    if normalized_route_id:
        entry = _lookup_entry(
            f"discord_autocomplete:{normalized_route_id}",
            ux_contract=ux_contract,
        )
        if entry is not None:
            return entry
        if normalized_route_id.startswith("car.flow.") and normalized_route_id.endswith(
            ".run_id"
        ):
            return _lookup_entry(
                "discord_autocomplete:flow.run_picker",
                ux_contract=ux_contract,
            )
    normalized_path = tuple(str(part or "").strip().lower() for part in command_path)
    if (
        len(normalized_path) == 3
        and normalized_path[:2] == ("car", "flow")
        and normalized_path[2]
        in {
            "status",
            "runs",
            "issue",
            "plan",
            "start",
            "restart",
            "resume",
            "stop",
            "archive",
            "recover",
            "reply",
        }
        and str(focused_name or "").strip().lower() == "run_id"
    ):
        return _lookup_entry(
            "discord_autocomplete:flow.run_picker",
            ux_contract=ux_contract,
        )
    return None


def plain_text_turn_ux_contract_for_mode(
    mode: str,
    *,
    ux_contract: Sequence[ChatActionUxContractEntry] = CHAT_ACTION_UX_CONTRACT,
) -> Optional[ChatActionUxContractEntry]:
    normalized = str(mode or "").strip()
    if not normalized:
        return None
    return _lookup_entry(f"plain_text_turn:{normalized}", ux_contract=ux_contract)


def discord_ack_policy_for_entry(
    entry: Optional[ChatActionUxContractEntry],
    *,
    dispatch: bool = False,
) -> Optional[DiscordAckPolicy]:
    if entry is None:
        return None
    ack_class = entry.dispatch_ack_class if dispatch else entry.ack_class
    if ack_class in {
        "immediate",
        "defer_ephemeral",
        "defer_public",
        "defer_component_update",
    }:
        return cast(DiscordAckPolicy, ack_class)
    return None


def discord_scheduler_ack_strategy_for_entry(
    entry: Optional[ChatActionUxContractEntry],
) -> SchedulerAckStrategy:
    if entry is None:
        return "none"
    if entry.ack_class == "defer_component_update":
        return "scheduler_component_update"
    if entry.ack_class == "defer_ephemeral":
        return "scheduler_ephemeral"
    return "none"


def discord_exposure_for_entry(
    entry: Optional[ChatActionUxContractEntry],
) -> Optional[DiscordExposure]:
    if entry is None:
        return None
    if entry.visibility in {"public", "operator"}:
        return cast(DiscordExposure, entry.visibility)
    return None


def telegram_exposure_for_entry(
    entry: Optional[ChatActionUxContractEntry],
) -> Optional[TelegramExposure]:
    if entry is None:
        return None
    if entry.visibility in {"public", "hidden", "legacy_alias"}:
        return cast(TelegramExposure, entry.visibility)
    return None


def telegram_response_policy_for_entry(
    entry: Optional[ChatActionUxContractEntry],
) -> Optional[TelegramResponsePolicy]:
    if entry is None or entry.ack_class != "typing":
        return None
    return "typing"


def telegram_allow_during_turn_for_entry(
    entry: Optional[ChatActionUxContractEntry],
) -> Optional[bool]:
    if entry is None or entry.surface != "telegram_command":
        return None
    return entry.queue_policy in {"allow_during_turn", "control_priority"}


def callback_entry_bypasses_queue(
    entry: Optional[ChatActionUxContractEntry],
) -> bool:
    if entry is None:
        return False
    return entry.queue_policy in {"bypass_active_turn", "control_priority"}


__all__ = [
    "CHAT_ACTION_UX_CONTRACT",
    "CHAT_ACTION_UX_CONTRACT_VERSION",
    "AnchorMessageReuse",
    "ChatActionAckClass",
    "ChatActionPriority",
    "ChatActionQueuePolicy",
    "ChatActionSurface",
    "ChatActionUxContractEntry",
    "ChatActionVisibility",
    "DiscordAckPolicy",
    "DiscordAckTiming",
    "DiscordExposure",
    "FirstVisibleFeedbackPolicy",
    "SchedulerAckStrategy",
    "TelegramExposure",
    "TelegramResponsePolicy",
    "callback_entry_bypasses_queue",
    "discord_ack_policy_for_entry",
    "discord_autocomplete_ux_contract_for_route",
    "discord_component_ux_contract_for_route",
    "discord_exposure_for_entry",
    "discord_modal_ux_contract_for_route",
    "discord_scheduler_ack_strategy_for_entry",
    "discord_slash_command_ux_contract_for_id",
    "plain_text_turn_ux_contract_for_mode",
    "telegram_allow_during_turn_for_entry",
    "telegram_callback_ux_contract_for_callback",
    "telegram_command_ux_contract_for_name",
    "telegram_exposure_for_entry",
    "telegram_response_policy_for_entry",
]
