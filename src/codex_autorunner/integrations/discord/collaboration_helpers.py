from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from ..chat.collaboration_policy import (
    CollaborationEvaluationContext,
    CollaborationEvaluationResult,
    CollaborationPolicy,
    evaluate_collaboration_policy,
)


def collaboration_probe_text(application_id: Optional[str]) -> str:
    token = str(application_id or "").strip()
    if token:
        return f"<@{token}> collaboration probe"
    return "collaboration probe"


def evaluate_collaboration_summary(
    service: object,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
) -> tuple[object, object]:
    evaluator = getattr(service, "_evaluate_channel_collaboration_summary", None)
    if callable(evaluator):
        return evaluator(
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )

    policy = getattr(service, "_collaboration_policy", None)
    if not isinstance(policy, CollaborationPolicy):
        fallback = SimpleNamespace(
            command_allowed=True,
            destination_mode="active",
            plain_text_trigger="always",
            reason="collaboration_unavailable",
            matched_destination=None,
        )
        return fallback, fallback

    build_context = getattr(service, "_build_plain_text_turn_context", None)
    probe_text = collaboration_probe_text(
        getattr(getattr(service, "_config", None), "application_id", None)
    )
    if callable(build_context):
        plain_text = build_context(text=probe_text, guild_id=guild_id)
    else:
        plain_text = SimpleNamespace(text=probe_text)

    command_result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id=user_id,
            container_id=guild_id,
            destination_id=channel_id,
            is_explicit_command=True,
        ),
    )
    plain_text_result = evaluate_collaboration_policy(
        policy,
        CollaborationEvaluationContext(
            actor_id=user_id,
            container_id=guild_id,
            destination_id=channel_id,
            plain_text=plain_text,
        ),
    )
    return command_result, plain_text_result


def collaboration_destination_label(
    channel_id: str,
    result: CollaborationEvaluationResult,
) -> str:
    matched = result.matched_destination
    if matched is not None and matched.name:
        return matched.name
    if matched is not None:
        return matched.destination_id
    return channel_id


def collaboration_command_behavior_label(
    result: CollaborationEvaluationResult,
) -> str:
    if result.command_allowed:
        return "allowed"
    return f"denied ({result.reason})"


def collaboration_plain_text_behavior_label(
    result: CollaborationEvaluationResult,
    *,
    binding: Optional[dict[str, object]],
) -> str:
    if result.destination_mode == "silent":
        return "disabled (silent destination)"
    if (
        result.destination_mode == "command_only"
        or result.plain_text_trigger == "disabled"
    ):
        return "disabled (commands only)"
    if binding is None:
        if result.plain_text_trigger == "mentions":
            return "mentions only after /car bind or /pma on"
        return "requires /car bind or /pma on"
    if bool(binding.get("pma_enabled", False)):
        if result.plain_text_trigger == "mentions":
            return "mentions only (PMA active)"
        return "all normal messages (PMA active)"
    if result.plain_text_trigger == "mentions":
        return "mentions only"
    return "all normal messages"


def collaboration_binding_label(binding: Optional[dict[str, object]]) -> str:
    if binding is None:
        return "unbound"
    if bool(binding.get("pma_enabled", False)):
        return "PMA enabled"
    workspace = str(binding.get("workspace_path") or "").strip()
    if workspace:
        return "bound workspace"
    return "binding configured"


def collaboration_summary_lines(
    *,
    channel_id: str,
    command_result: CollaborationEvaluationResult,
    plain_text_result: CollaborationEvaluationResult,
    binding: Optional[dict[str, object]],
) -> list[str]:
    return [
        f"Policy destination: {collaboration_destination_label(channel_id, plain_text_result)}",
        f"Policy mode: {plain_text_result.destination_mode}",
        f"Policy plain-text trigger: {plain_text_result.plain_text_trigger}",
        f"Policy commands: {collaboration_command_behavior_label(command_result)}",
        f"Policy plain-text: {collaboration_plain_text_behavior_label(plain_text_result, binding=binding)}",
        f"Binding/PMA: {collaboration_binding_label(binding)}",
    ]


def build_collaboration_snippet_lines(
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
) -> list[str]:
    lines = [
        "Suggested collaboration config:",
        "collaboration_policy:",
        "  discord:",
    ]
    if guild_id:
        lines.append(f"    allowed_guild_ids: [{guild_id}]")
    else:
        lines.append(f"    allowed_channel_ids: [{channel_id}]")
    if user_id:
        lines.append(f"    allowed_user_ids: [{user_id}]")
    lines.extend(
        [
            "    default_mode: command_only",
            "    destinations:",
            (
                "      - { "
                + (f"guild_id: {guild_id}, " if guild_id else "")
                + f"channel_id: {channel_id}, mode: active, plain_text_trigger: mentions }}"
            ),
            (
                "      - { "
                + (f"guild_id: {guild_id}, " if guild_id else "")
                + "channel_id: HUMAN_ONLY_CHANNEL_ID, mode: silent }"
            ),
            "    # default_mode: command_only keeps slash commands available in other allowlisted channels without passive replies.",
        ]
    )
    return lines
