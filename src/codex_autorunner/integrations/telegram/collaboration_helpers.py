from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from ..chat.collaboration_policy import CollaborationEvaluationResult
from .adapter import TelegramMessage


def collaboration_probe_text(bot_username: Optional[str]) -> str:
    if isinstance(bot_username, str) and bot_username.strip():
        return f"@{bot_username.strip()} collaboration probe"
    return "collaboration probe"


def evaluate_collaboration_summary(
    handlers: object,
    message: TelegramMessage,
    *,
    command_text: str,
) -> tuple[object, object]:
    evaluator = getattr(handlers, "_evaluate_collaboration_message_policy", None)
    if callable(evaluator):
        return (
            evaluator(
                message,
                text=command_text,
                is_explicit_command=True,
            ),
            evaluator(
                message,
                text=collaboration_probe_text(getattr(handlers, "_bot_username", None)),
                is_explicit_command=False,
            ),
        )
    fallback = SimpleNamespace(
        command_allowed=True,
        destination_mode="command_only",
        plain_text_trigger="disabled",
        reason="collaboration_unavailable",
        matched_destination=None,
    )
    return fallback, fallback


def collaboration_scope_label(message: TelegramMessage) -> str:
    if message.thread_id is None:
        return "root chat"
    return f"topic {message.thread_id}"


def collaboration_destination_label(
    message: TelegramMessage,
    result: CollaborationEvaluationResult,
) -> str:
    matched = result.matched_destination
    if matched is not None and matched.name:
        return matched.name
    if matched is not None and matched.subdestination_id is not None:
        return f"topic {matched.subdestination_id}"
    if matched is not None:
        return "root chat"
    return collaboration_scope_label(message)


def collaboration_command_behavior_label(
    result: CollaborationEvaluationResult,
) -> str:
    if result.command_allowed:
        return "allowed"
    if result.reason == "subdestination_required":
        return "denied in root chat (require_topics=true)"
    return f"denied ({result.reason})"


def collaboration_plain_text_behavior_label(
    result: CollaborationEvaluationResult,
) -> str:
    if result.destination_mode == "silent":
        return "disabled (silent destination)"
    if result.reason == "subdestination_required":
        return "disabled in root chat (require_topics=true)"
    if (
        result.destination_mode == "command_only"
        or result.plain_text_trigger == "disabled"
    ):
        return "disabled"
    if result.plain_text_trigger == "mentions":
        return "mentions or replies to the bot only"
    return "all normal messages"


def collaboration_summary_lines(
    message: TelegramMessage,
    *,
    command_result: CollaborationEvaluationResult,
    plain_text_result: CollaborationEvaluationResult,
) -> list[str]:
    return [
        f"Location: {collaboration_scope_label(message)}",
        f"Policy destination: {collaboration_destination_label(message, plain_text_result)}",
        f"Policy mode: {plain_text_result.destination_mode}",
        f"Policy plain-text trigger: {plain_text_result.plain_text_trigger}",
        f"Policy commands: {collaboration_command_behavior_label(command_result)}",
        f"Policy plain-text: {collaboration_plain_text_behavior_label(plain_text_result)}",
    ]


def build_collaboration_snippet_lines(message: TelegramMessage) -> list[str]:
    trigger = _recommended_collaboration_trigger(message)
    lines = [
        "Suggested collaboration config:",
        "collaboration_policy:",
        "  telegram:",
        f"    allowed_chat_ids: [{message.chat_id}]",
    ]
    if message.from_user_id is not None:
        lines.append(f"    allowed_user_ids: [{message.from_user_id}]")
    if message.thread_id is not None:
        lines.extend(
            [
                "    require_topics: true",
                "    destinations:",
                (
                    "      - { chat_id: "
                    f"{message.chat_id}, thread_id: {message.thread_id}, "
                    f"mode: active, plain_text_trigger: {trigger} }}"
                ),
                (
                    "      - { chat_id: "
                    f"{message.chat_id}, mode: silent }}  # keep the root chat human-only"
                ),
                "    # Change the topic mode above to command_only or silent as needed.",
            ]
        )
        return lines

    lines.extend(
        [
            "    destinations:",
            (
                "      - { chat_id: "
                f"{message.chat_id}, mode: active, plain_text_trigger: {trigger} }}"
            ),
            "    # Use mode: command_only or mode: silent to reduce root-chat noise.",
            "    # For topic-only collaboration, enable forum topics and rerun /ids inside the topic.",
        ]
    )
    return lines


def _recommended_collaboration_trigger(message: TelegramMessage) -> str:
    if message.thread_id is not None:
        return "mentions"
    if message.chat_type in {"group", "supergroup"}:
        return "mentions"
    return "always"
