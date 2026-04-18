from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from ...chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn
from ..trigger_mode import should_trigger_run


def evaluate_message_policy(
    handlers: Any,
    message: Any,
    *,
    text: str,
    is_explicit_command: bool,
) -> Any:
    evaluator = getattr(handlers, "_evaluate_collaboration_message_policy", None)
    if callable(evaluator):
        return evaluator(
            message,
            text=text,
            is_explicit_command=is_explicit_command,
        )
    if is_explicit_command:
        return SimpleNamespace(command_allowed=True, should_start_turn=False)
    trigger_mode = getattr(getattr(handlers, "_config", None), "trigger_mode", "all")
    if trigger_mode == "mentions" and not should_trigger_run(
        message,
        text=text,
        bot_username=getattr(handlers, "_bot_username", None),
    ):
        return SimpleNamespace(command_allowed=True, should_start_turn=False)
    return SimpleNamespace(command_allowed=True, should_start_turn=True)


def log_message_policy_result(handlers: Any, message: Any, result: Any) -> None:
    logger = getattr(handlers, "_log_collaboration_policy_result", None)
    if callable(logger):
        logger(message, result)


def activated_record_allows_plain_text_turn(
    handlers: Any,
    message: Any,
    *,
    text: str,
    record: Any,
    policy_result: Any,
) -> bool:
    if record is None:
        return False
    if not (
        bool(getattr(record, "pma_enabled", False))
        or bool(getattr(record, "workspace_path", None))
    ):
        return False
    if getattr(policy_result, "matched_destination", None) is not None:
        return False
    if getattr(policy_result, "destination_mode", None) != "command_only":
        return False
    if getattr(policy_result, "reason", None) != "plain_text_disabled":
        return False

    default_trigger = getattr(
        getattr(handlers, "_collaboration_policy", None),
        "default_plain_text_trigger",
        "always",
    )
    if default_trigger == "disabled":
        return False
    if default_trigger not in {"always", "mentions"}:
        return False
    return should_trigger_plain_text_turn(
        mode=default_trigger,
        context=PlainTextTurnContext(
            text=text,
            chat_type=message.chat_type,
            bot_username=getattr(handlers, "_bot_username", None),
            reply_to_is_bot=message.reply_to_is_bot,
            reply_to_username=message.reply_to_username,
            reply_to_message_id=(
                str(message.reply_to_message_id)
                if message.reply_to_message_id is not None
                else None
            ),
            thread_id=str(message.thread_id) if message.thread_id is not None else None,
        ),
    )


def event_logger(handlers: Any) -> logging.Logger:
    candidate = getattr(handlers, "_logger", None)
    if hasattr(candidate, "log"):
        return candidate
    return logging.getLogger(__name__)
