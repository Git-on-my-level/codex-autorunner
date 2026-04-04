from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from ....core.logging_utils import log_event
from ....core.orchestration import (
    FlowTarget,
    PausedFlowTarget,
    SurfaceThreadMessageRequest,
    build_surface_orchestration_ingress,
)
from ....core.pma_notification_store import (
    PmaNotificationStore,
    build_notification_context_block,
    notification_surface_key,
)
from ....core.utils import canonicalize_path
from ....integrations.chat.constants import TOPIC_NOT_BOUND_MESSAGE
from ...chat.forwarding import compose_forwarded_message_text
from ...chat.handlers.messages import message_text_candidate
from ...chat.media import audio_content_type_for_input, is_image_mime_or_path
from ...chat.turn_policy import PlainTextTurnContext, should_trigger_plain_text_turn
from ..adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    is_interrupt_alias,
    parse_command,
)
from ..config import TelegramMediaCandidate
from ..constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ..forwarding import (
    format_forwarded_telegram_message_text,
    is_forwarded_telegram_message,
    message_forward_info,
)
from ..state import TelegramTopicRecord
from ..trigger_mode import should_trigger_run
from .commands.execution import _build_telegram_thread_orchestration_service
from .questions import handle_custom_text_input

COALESCE_LONG_MESSAGE_WINDOW_SECONDS = 6.0
COALESCE_LONG_MESSAGE_THRESHOLD = TELEGRAM_MAX_MESSAGE_LENGTH - 256
MEDIA_BATCH_WINDOW_SECONDS = 1.0
MAX_BATCH_ITEMS = 10


def _evaluate_message_policy(
    handlers: Any,
    message: TelegramMessage,
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


def _log_message_policy_result(
    handlers: Any, message: TelegramMessage, result: Any
) -> None:
    logger = getattr(handlers, "_log_collaboration_policy_result", None)
    if callable(logger):
        logger(message, result)


def _event_logger(handlers: Any) -> logging.Logger:
    candidate = getattr(handlers, "_logger", None)
    if hasattr(candidate, "log"):
        return candidate
    return logging.getLogger(__name__)


async def _run_with_typing_indicator(
    handlers: Any,
    *,
    chat_id: Optional[int],
    thread_id: Optional[int],
    work: Any,
) -> None:
    if chat_id is None:
        await work()
        return
    begin = getattr(handlers, "_begin_typing_indicator", None)
    end = getattr(handlers, "_end_typing_indicator", None)
    began = False
    if callable(begin):
        try:
            await begin(chat_id, thread_id)
            began = True
        except Exception:
            began = False
    try:
        await work()
    finally:
        if began and callable(end):
            try:
                await end(chat_id, thread_id)
            except Exception:
                pass


def _paused_flow_status(run_record: Any) -> str:
    status = getattr(run_record, "status", None)
    if status is None:
        return "paused"
    return str(getattr(status, "value", status))


def _has_pending_custom_question(handlers: Any, message: TelegramMessage) -> bool:
    for pending in handlers._pending_questions.values():
        if (
            pending.awaiting_custom_input
            and pending.chat_id == message.chat_id
            and (pending.thread_id is None or pending.thread_id == message.thread_id)
        ):
            return True
    return False


def _pending_state_belongs_to_actor(state: Any, actor_id: Optional[str]) -> bool:
    if state is None:
        return False
    if isinstance(state, dict):
        expected = state.get("requester_user_id")
    else:
        expected = getattr(state, "requester_user_id", None)
    if expected is None:
        return True
    return expected == actor_id


def _pop_pending_state_if_owned(
    state_map: dict[str, Any],
    key: str,
    actor_id: Optional[str],
) -> Any:
    state = state_map.get(key)
    if not _pending_state_belongs_to_actor(state, actor_id):
        return None
    return state_map.pop(key, None)


@dataclass
class _CoalescedBuffer:
    message: TelegramMessage
    parts: list[str]
    topic_key: str
    placeholder_id: Optional[int] = None
    task: Optional[asyncio.Task[None]] = None
    last_received_at: float = 0.0
    last_part_len: int = 0


@dataclass
class _MediaBatchBuffer:
    topic_key: str
    messages: list[TelegramMessage] = field(default_factory=list)
    placeholder_id: Optional[int] = None
    task: Optional[asyncio.Task[None]] = None
    media_group_id: Optional[str] = None
    created_at: float = 0.0


def _message_text_candidate(message: TelegramMessage) -> tuple[str, str, Any]:
    return message_text_candidate(
        text=message.text,
        caption=message.caption,
        entities=message.entities,
        caption_entities=message.caption_entities,
    )


def _record_with_media_workspace(
    handlers: Any, record: Any
) -> tuple[Any, Optional[str]]:
    """Ensure media handlers have a workspace path, including PMA topics."""
    if record is None:
        return None, None
    pma_enabled = bool(getattr(record, "pma_enabled", False))
    if not pma_enabled:
        return record, None
    hub_root = getattr(handlers, "_hub_root", None)
    if hub_root is None:
        return None, "PMA unavailable; hub root not configured."
    return dataclasses.replace(record, workspace_path=str(hub_root)), None


def _activated_record_allows_plain_text_turn(
    handlers: Any,
    message: TelegramMessage,
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


async def _clear_pending_options(
    handlers: Any, key: str, message: TelegramMessage
) -> None:
    agent_profile_options = getattr(handlers, "_agent_profile_options", None)
    handlers._resume_options.pop(key, None)
    handlers._bind_options.pop(key, None)
    handlers._agent_options.pop(key, None)
    if isinstance(agent_profile_options, dict):
        agent_profile_options.pop(key, None)
    handlers._model_options.pop(key, None)
    handlers._model_pending.pop(key, None)
    handlers._review_commit_options.pop(key, None)
    handlers._review_commit_subjects.pop(key, None)
    pending_review_custom = handlers._pending_review_custom.pop(key, None)
    await handlers._dismiss_review_custom_prompt(message, pending_review_custom)


async def _enqueue_or_run_topic_work(
    handlers: Any,
    key: str,
    *,
    chat_id: int,
    thread_id: Optional[int],
    placeholder_id: Optional[int],
    work: Any,
) -> None:
    async def _run_wrapped() -> None:
        await _run_placeholder_wrapped_work(
            handlers,
            chat_id=chat_id,
            placeholder_id=placeholder_id,
            work=work,
        )

    async def _run_wrapped_with_typing() -> None:
        await _run_with_typing_indicator(
            handlers,
            chat_id=chat_id,
            thread_id=thread_id,
            work=_run_wrapped,
        )

    enqueue = getattr(handlers, "_enqueue_topic_work", None)
    if callable(enqueue):
        enqueue(key, _run_wrapped_with_typing)
        return
    await _run_wrapped_with_typing()


async def _run_placeholder_wrapped_work(
    handlers: Any,
    *,
    chat_id: int,
    placeholder_id: Optional[int],
    work: Any,
) -> None:
    wrapped = work
    wrap = getattr(handlers, "_wrap_placeholder_work", None)
    if callable(wrap):
        wrapped = wrap(
            chat_id=chat_id,
            placeholder_id=placeholder_id,
            work=work,
        )
    if callable(wrapped):
        await wrapped()
        return
    await wrapped


async def handle_message(handlers: Any, message: TelegramMessage) -> None:
    placeholder_id = handlers._claim_queued_placeholder(
        message.chat_id, message.message_id
    )
    if message.is_edited:
        await handle_edited_message(handlers, message, placeholder_id=placeholder_id)
        return

    _raw_text, text_candidate, entities = _message_text_candidate(message)
    trimmed_text = text_candidate.strip()
    has_media = message_has_media(message)
    is_forwarded = is_forwarded_telegram_message(message)
    if not trimmed_text and not has_media:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return

    if trimmed_text and not has_media:
        if _has_pending_custom_question(handlers, message):
            policy_result = _evaluate_message_policy(
                handlers,
                message,
                text=trimmed_text,
                is_explicit_command=not is_forwarded,
            )
            if not policy_result.command_allowed:
                _log_message_policy_result(handlers, message, policy_result)
                if placeholder_id is not None:
                    await handlers._delete_message(message.chat_id, placeholder_id)
                return
        custom_handled = await handle_custom_text_input(handlers, message)
        if custom_handled:
            return

    should_bypass = False
    if trimmed_text:
        if is_interrupt_alias(trimmed_text) and not is_forwarded:
            should_bypass = True
        elif trimmed_text.startswith("!") and not has_media and not is_forwarded:
            should_bypass = True
        elif not is_forwarded and parse_command(
            text_candidate, entities=entities, bot_username=handlers._bot_username
        ):
            should_bypass = True

    if has_media and not should_bypass:
        if handlers._config.media.batch_uploads and has_batchable_media(message):
            if handlers._config.media.enabled:
                topic_key = await handlers._resolve_topic_key(
                    message.chat_id, message.thread_id
                )
                await _clear_pending_options(handlers, topic_key, message)
                await flush_coalesced_message(handlers, message)
                await buffer_media_batch(
                    handlers, message, placeholder_id=placeholder_id
                )
                return
            should_bypass = True
        else:
            should_bypass = True

    if should_bypass:
        await flush_coalesced_message(handlers, message)
        await handle_message_inner(handlers, message, placeholder_id=placeholder_id)
        return

    if is_forwarded:
        await flush_coalesced_message(handlers, message)
        await handle_message_inner(handlers, message, placeholder_id=placeholder_id)
        return

    await buffer_coalesced_message(
        handlers, message, text_candidate, placeholder_id=placeholder_id
    )


def should_bypass_topic_queue(handlers: Any, message: TelegramMessage) -> bool:
    for pending in handlers._pending_questions.values():
        if (
            pending.awaiting_custom_input
            and pending.chat_id == message.chat_id
            and (pending.thread_id is None or pending.thread_id == message.thread_id)
        ):
            return True
    _raw_text, text_candidate, entities = _message_text_candidate(message)
    if not text_candidate:
        return False
    trimmed_text = text_candidate.strip()
    if not trimmed_text:
        return False
    if is_forwarded_telegram_message(message):
        return False
    if is_interrupt_alias(trimmed_text):
        return True
    command = parse_command(
        text_candidate, entities=entities, bot_username=handlers._bot_username
    )
    if not command:
        return False
    spec = handlers._command_specs.get(command.name)
    return bool(spec and spec.allow_during_turn)


async def handle_edited_message(
    handlers: Any,
    message: TelegramMessage,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    text = (message.text or "").strip()
    if not text:
        text = (message.caption or "").strip()
    if not text:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    runtime = handlers._router.runtime_for(key)
    turn_key = runtime.current_turn_key
    if not turn_key:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return
    ctx = handlers._turn_contexts.get(turn_key)
    if ctx is None or ctx.reply_to_message_id != message.message_id:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return
    await handlers._handle_interrupt(message, runtime)
    edited_text = f"Edited: {text}"

    async def work() -> None:
        await handlers._handle_normal_message(
            message,
            runtime,
            text_override=edited_text,
            placeholder_id=placeholder_id,
        )

    await _enqueue_or_run_topic_work(
        handlers,
        key,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        placeholder_id=placeholder_id,
        work=work,
    )


async def handle_message_inner(
    handlers: Any,
    message: TelegramMessage,
    *,
    topic_key: Optional[str] = None,
    placeholder_id: Optional[int] = None,
) -> None:
    raw_text = message.text or ""
    raw_caption = message.caption or ""
    text = raw_text.strip()
    entities = message.entities
    if not text:
        text = raw_caption.strip()
        entities = message.caption_entities
    has_media = message_has_media(message)
    if not text and not has_media:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)
        return

    async def _clear_placeholder() -> None:
        if placeholder_id is not None:
            await handlers._delete_message(message.chat_id, placeholder_id)

    if isinstance(topic_key, str) and topic_key:
        key = topic_key
    else:
        key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    runtime = handlers._router.runtime_for(key)
    actor_id = str(message.from_user_id) if message.from_user_id is not None else None
    is_forwarded = is_forwarded_telegram_message(message)
    turn_text = format_forwarded_telegram_message_text(message, text)
    flow_reply_text = compose_forwarded_message_text(
        text,
        message_forward_info(message, text),
    )

    command_policy_result = _evaluate_message_policy(
        handlers,
        message,
        text=text,
        is_explicit_command=not is_forwarded,
    )

    if text and not command_policy_result.command_allowed:
        has_pending_state = (
            bool(handlers._resume_options.get(key))
            or bool(handlers._bind_options.get(key))
            or bool(handlers._review_commit_options.get(key))
            or bool(handlers._pending_review_custom.get(key))
        )
        if has_pending_state:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_placeholder()
            return

    if text and handlers._handle_pending_resume(
        key,
        text,
        user_id=message.from_user_id,
    ):
        await _clear_placeholder()
        return
    if text and handlers._handle_pending_bind(
        key,
        text,
        user_id=message.from_user_id,
    ):
        await _clear_placeholder()
        return

    if text and is_interrupt_alias(text) and not is_forwarded:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_placeholder()
            return
        await handlers._handle_interrupt(message, runtime)
        await _clear_placeholder()
        return

    if text and text.startswith("!") and not has_media and not is_forwarded:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_placeholder()
            return
        agent_profile_options = getattr(handlers, "_agent_profile_options", None)
        _pop_pending_state_if_owned(handlers._resume_options, key, actor_id)
        _pop_pending_state_if_owned(handlers._bind_options, key, actor_id)
        handlers._flow_run_options.pop(key, None)
        handlers._agent_options.pop(key, None)
        if isinstance(agent_profile_options, dict):
            agent_profile_options.pop(key, None)
        handlers._model_options.pop(key, None)
        handlers._model_pending.pop(key, None)

        async def work() -> None:
            await handlers._handle_bang_shell(message, text, runtime)

        await _enqueue_or_run_topic_work(
            handlers,
            key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            placeholder_id=placeholder_id,
            work=work,
        )
        return

    if text and await handlers._handle_pending_review_commit(
        message,
        runtime,
        key,
        text,
    ):
        await _clear_placeholder()
        return

    command_text = raw_text if raw_text.strip() else raw_caption
    command = (
        parse_command(
            command_text, entities=entities, bot_username=handlers._bot_username
        )
        if command_text and not is_forwarded
        else None
    )
    if await handlers._handle_pending_review_custom(
        key, message, runtime, command, raw_text, raw_caption
    ):
        await _clear_placeholder()
        return
    if command:
        agent_profile_options = getattr(handlers, "_agent_profile_options", None)
        if command.name != "resume":
            _pop_pending_state_if_owned(handlers._resume_options, key, actor_id)
        if command.name != "bind":
            _pop_pending_state_if_owned(handlers._bind_options, key, actor_id)
        if command.name != "agent":
            handlers._agent_options.pop(key, None)
            if isinstance(agent_profile_options, dict):
                agent_profile_options.pop(key, None)
        if command.name != "model":
            handlers._model_options.pop(key, None)
            handlers._model_pending.pop(key, None)
        if command.name != "review":
            review_state = _pop_pending_state_if_owned(
                handlers._review_commit_options,
                key,
                actor_id,
            )
            if review_state is not None:
                handlers._review_commit_subjects.pop(key, None)
            pending_review_custom = _pop_pending_state_if_owned(
                handlers._pending_review_custom,
                key,
                actor_id,
            )
            await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    else:
        agent_profile_options = getattr(handlers, "_agent_profile_options", None)
        _pop_pending_state_if_owned(handlers._resume_options, key, actor_id)
        _pop_pending_state_if_owned(handlers._bind_options, key, actor_id)
        handlers._agent_options.pop(key, None)
        if isinstance(agent_profile_options, dict):
            agent_profile_options.pop(key, None)
        handlers._model_options.pop(key, None)
        handlers._model_pending.pop(key, None)
        review_state = _pop_pending_state_if_owned(
            handlers._review_commit_options,
            key,
            actor_id,
        )
        if review_state is not None:
            handlers._review_commit_subjects.pop(key, None)
        pending_review_custom = _pop_pending_state_if_owned(
            handlers._pending_review_custom,
            key,
            actor_id,
        )
        await handlers._dismiss_review_custom_prompt(message, pending_review_custom)
    if command:
        if not command_policy_result.command_allowed:
            _log_message_policy_result(handlers, message, command_policy_result)
            await _clear_placeholder()
            return
        spec = handlers._command_specs.get(command.name)

        async def work() -> None:
            await handlers._handle_command(command, message, runtime)

        if spec and spec.allow_during_turn:
            handlers._spawn_task(
                _run_placeholder_wrapped_work(
                    handlers,
                    chat_id=message.chat_id,
                    placeholder_id=placeholder_id,
                    work=work,
                )
            )
        else:
            await _enqueue_or_run_topic_work(
                handlers,
                key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=placeholder_id,
                work=work,
            )
        return

    record = await handlers._router.get_topic(key)
    paused = None
    workspace_root: Optional[Path] = None
    pma_enabled = bool(record and getattr(record, "pma_enabled", False))
    notification_reply = None
    notification_store_root = getattr(getattr(handlers, "_config", None), "root", None)
    if message.reply_to_message_id is not None and isinstance(
        notification_store_root, (str, Path)
    ):
        notification_reply = PmaNotificationStore(
            Path(notification_store_root)
        ).get_reply_target(
            surface_kind="telegram",
            surface_key=key,
            delivered_message_id=message.reply_to_message_id,
        )
    if not pma_enabled and record and record.workspace_path:
        workspace_root = canonicalize_path(Path(record.workspace_path))
        preferred_run_id = handlers._ticket_flow_pause_targets.get(
            str(workspace_root), None
        )
        paused = handlers._get_paused_ticket_flow(
            workspace_root, preferred_run_id=preferred_run_id
        )
    policy_result = _evaluate_message_policy(
        handlers,
        message,
        text=text,
        is_explicit_command=False,
    )
    if not paused and not policy_result.should_start_turn:
        if _activated_record_allows_plain_text_turn(
            handlers,
            message,
            text=text,
            record=record,
            policy_result=policy_result,
        ):
            pass
        else:
            _log_message_policy_result(handlers, message, policy_result)
            await _clear_placeholder()
            return

    if has_media:

        async def work() -> None:
            await handle_media_message(
                handlers,
                message,
                runtime,
                text,
                placeholder_id=placeholder_id,
            )

        await _enqueue_or_run_topic_work(
            handlers,
            key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            placeholder_id=placeholder_id,
            work=work,
        )
        return

    event_logger = _event_logger(handlers)
    ingress = build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: log_event(
            event_logger,
            logging.INFO,
            f"telegram.{orchestration_event.event_type}",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )

    async def _resolve_paused_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> Optional[PausedFlowTarget]:
        if paused is None:
            return None
        run_id, _run_record = paused
        return PausedFlowTarget(
            flow_target=FlowTarget(
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                display_name="ticket_flow",
                workspace_root=str(workspace_root or Path(".")),
            ),
            run_id=run_id,
            status=_paused_flow_status(_run_record),
            workspace_root=workspace_root or Path("."),
        )

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, flow_target: PausedFlowTarget
    ) -> None:
        if paused is None:
            return
        run_id, run_record = paused
        success, result = await handlers._write_user_reply_from_telegram(
            workspace_root or Path("."),
            run_id,
            run_record,
            message,
            flow_reply_text,
        )
        await handlers._send_message(
            message.chat_id,
            result,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        if success:
            await handlers._ticket_flow_bridge.auto_resume_run(
                workspace_root or Path("."), flow_target.run_id
            )

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> None:
        normal_message_kwargs: dict[str, Any] = {
            "text_override": turn_text,
            "placeholder_id": placeholder_id,
        }
        if notification_reply is not None:
            try:
                handle_normal_parameters = inspect.signature(
                    handlers._handle_normal_message
                ).parameters
            except (TypeError, ValueError):
                handle_normal_parameters = {}
            if "record" in handle_normal_parameters:
                normal_message_kwargs["record"] = dataclasses.replace(
                    record or TelegramTopicRecord(),
                    pma_enabled=True,
                    repo_id=notification_reply.repo_id
                    or getattr(record, "repo_id", None),
                    workspace_path=(
                        notification_reply.workspace_root
                        or str(getattr(handlers, "_hub_root", None) or "")
                        or getattr(record, "workspace_path", None)
                    ),
                )
            if "surface_key_override" in handle_normal_parameters:
                normal_message_kwargs["surface_key_override"] = (
                    notification_surface_key(notification_reply.notification_id)
                )
            if "pma_context_prefix" in handle_normal_parameters:
                normal_message_kwargs["pma_context_prefix"] = (
                    build_notification_context_block(notification_reply)
                )
        await handlers._handle_normal_message(message, runtime, **normal_message_kwargs)

    async def work() -> None:
        if notification_reply is not None:
            await _submit_thread_message(
                SurfaceThreadMessageRequest(
                    surface_kind="telegram",
                    workspace_root=workspace_root or Path("."),
                    prompt_text=turn_text,
                    agent_id=getattr(record, "agent", None),
                    pma_enabled=True,
                )
            )
            orch_binding = _build_telegram_thread_orchestration_service(
                handlers
            ).get_binding(
                surface_kind="telegram",
                surface_key=notification_surface_key(
                    notification_reply.notification_id
                ),
            )
            if orch_binding is not None:
                PmaNotificationStore(handlers._config.root).bind_continuation_thread(
                    notification_id=notification_reply.notification_id,
                    thread_target_id=orch_binding.thread_target_id,
                )
            return
        await ingress.submit_message(
            SurfaceThreadMessageRequest(
                surface_kind="telegram",
                workspace_root=workspace_root or Path("."),
                prompt_text=turn_text,
                agent_id=getattr(record, "agent", None),
                pma_enabled=pma_enabled,
            ),
            resolve_paused_flow_target=_resolve_paused_flow,
            submit_flow_reply=_submit_flow_reply,
            submit_thread_message=_submit_thread_message,
        )

    await _enqueue_or_run_topic_work(
        handlers,
        key,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        placeholder_id=placeholder_id,
        work=work,
    )


def coalesce_key_for_topic(handlers: Any, key: str, user_id: Optional[int]) -> str:
    if user_id is None:
        return f"{key}:user:unknown"
    return f"{key}:user:{user_id}"


async def coalesce_key(handlers: Any, message: TelegramMessage) -> str:
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    return coalesce_key_for_topic(handlers, key, message.from_user_id)


async def _ensure_key_lock(
    handlers: Any,
    *,
    locks_attr: str,
    guard_attr: str,
    key: str,
) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = getattr(handlers, locks_attr)
    lock = locks.get(key)
    if lock is not None:
        return lock
    guard = getattr(handlers, guard_attr, None)
    if guard is None:
        guard = asyncio.Lock()
        setattr(handlers, guard_attr, guard)
    async with guard:
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
    return lock


async def buffer_coalesced_message(
    handlers: Any,
    message: TelegramMessage,
    text: str,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = coalesce_key_for_topic(handlers, topic_key, message.from_user_id)
    lock = await _ensure_key_lock(
        handlers,
        locks_attr="_coalesce_locks",
        guard_attr="_coalesce_locks_guard",
        key=key,
    )
    drop_placeholder = False
    async with lock:
        now = time.monotonic()
        buffer = handlers._coalesced_buffers.get(key)
        if buffer is None:
            buffer = _CoalescedBuffer(
                message=message,
                parts=[text],
                topic_key=topic_key,
                placeholder_id=placeholder_id,
                last_received_at=now,
                last_part_len=len(text),
            )
            handlers._coalesced_buffers[key] = buffer
        else:
            buffer.parts.append(text)
            buffer.last_received_at = now
            buffer.last_part_len = len(text)
            if placeholder_id is not None:
                if buffer.placeholder_id is None:
                    buffer.placeholder_id = placeholder_id
                else:
                    drop_placeholder = True
        handlers._touch_cache_timestamp("coalesced_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        window_seconds = handlers._config.coalesce_window_seconds
        buffer.task = handlers._spawn_task(
            coalesce_flush_after(handlers, key, window_seconds)
        )
    if drop_placeholder and placeholder_id is not None:
        await handlers._delete_message(message.chat_id, placeholder_id)


async def coalesce_flush_after(handlers: Any, key: str, window_seconds: float) -> None:
    try:
        await asyncio.sleep(window_seconds)
    except asyncio.CancelledError:
        return
    try:
        while True:
            buffer = handlers._coalesced_buffers.get(key)
            if buffer is None:
                return
            if buffer.last_part_len >= COALESCE_LONG_MESSAGE_THRESHOLD:
                elapsed = time.monotonic() - buffer.last_received_at
                long_window = max(window_seconds, COALESCE_LONG_MESSAGE_WINDOW_SECONDS)
                remaining = long_window - elapsed
                if remaining > 0:
                    try:
                        await asyncio.sleep(remaining)
                    except asyncio.CancelledError:
                        return
                    continue
            break
        await flush_coalesced_key(handlers, key)
    except Exception as exc:
        log_event(
            handlers._logger,
            logging.WARNING,
            "telegram.coalesce.flush_failed",
            key=key,
            exc=exc,
        )


async def flush_coalesced_message(handlers: Any, message: TelegramMessage) -> None:
    await flush_coalesced_key(handlers, await coalesce_key(handlers, message))


async def flush_coalesced_key(handlers: Any, key: str) -> None:
    lock = handlers._coalesce_locks.get(key)
    if lock is None:
        return
    buffer = None
    async with lock:
        buffer = handlers._coalesced_buffers.pop(key, None)
    if buffer is None:
        return
    task = buffer.task
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    combined_message = build_coalesced_message(buffer)

    async def _handle() -> None:
        await handle_message_inner(
            handlers,
            combined_message,
            topic_key=buffer.topic_key,
            placeholder_id=buffer.placeholder_id,
        )

    await _run_with_typing_indicator(
        handlers,
        chat_id=combined_message.chat_id,
        thread_id=combined_message.thread_id,
        work=_handle,
    )


def build_coalesced_message(buffer: _CoalescedBuffer) -> TelegramMessage:
    combined_text = "\n".join(buffer.parts)
    return dataclasses.replace(buffer.message, text=combined_text, caption=None)


def message_has_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document or message.voice or message.audio)


def select_photo(
    photos: Sequence[TelegramPhotoSize],
) -> Optional[TelegramPhotoSize]:
    if not photos:
        return None
    return max(
        photos,
        key=lambda item: ((item.file_size or 0), item.width * item.height),
    )


def document_is_image(document: TelegramDocument) -> bool:
    return is_image_mime_or_path(document.mime_type, document.file_name)


def select_image_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    photo = select_photo(message.photos)
    if photo:
        return TelegramMediaCandidate(
            kind="photo",
            file_id=photo.file_id,
            file_name=None,
            mime_type=None,
            file_size=photo.file_size,
        )
    if message.document and document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="document",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def select_voice_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.voice:
        voice = message.voice
        mime_type = audio_content_type_for_input(
            mime_type=voice.mime_type,
            file_name=None,
            source_url=None,
        )
        return TelegramMediaCandidate(
            kind="voice",
            file_id=voice.file_id,
            file_name=None,
            mime_type=mime_type,
            file_size=voice.file_size,
            duration=voice.duration,
        )
    if message.audio:
        audio = message.audio
        mime_type = audio_content_type_for_input(
            mime_type=audio.mime_type,
            file_name=audio.file_name,
            source_url=None,
        )
        return TelegramMediaCandidate(
            kind="audio",
            file_id=audio.file_id,
            file_name=audio.file_name,
            mime_type=mime_type,
            file_size=audio.file_size,
            duration=audio.duration,
        )
    return None


def select_file_candidate(
    message: TelegramMessage,
) -> Optional[TelegramMediaCandidate]:
    if message.document and not document_is_image(message.document):
        document = message.document
        return TelegramMediaCandidate(
            kind="file",
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )
    return None


def has_batchable_media(message: TelegramMessage) -> bool:
    return bool(message.photos or message.document)


async def media_batch_key(handlers: Any, message: TelegramMessage) -> str:
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    user_id = message.from_user_id
    if message.media_group_id:
        return f"{topic_key}:user:{user_id}:mg:{message.media_group_id}"
    return f"{topic_key}:user:{user_id}:burst"


async def buffer_media_batch(
    handlers: Any,
    message: TelegramMessage,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    if not has_batchable_media(message):
        return
    topic_key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    key = await media_batch_key(handlers, message)
    lock = await _ensure_key_lock(
        handlers,
        locks_attr="_media_batch_locks",
        guard_attr="_media_batch_locks_guard",
        key=key,
    )
    drop_placeholder = False
    async with lock:
        buffer = handlers._media_batch_buffers.get(key)
        if buffer is not None and len(buffer.messages) >= MAX_BATCH_ITEMS:
            if buffer.task and buffer.task is not asyncio.current_task():
                buffer.task.cancel()

            async def work(
                msgs: list[TelegramMessage] = buffer.messages,
                pid: Optional[int] = buffer.placeholder_id,
            ) -> None:
                await handlers._handle_media_batch(msgs, placeholder_id=pid)

            await _enqueue_or_run_topic_work(
                handlers,
                buffer.topic_key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=buffer.placeholder_id,
                work=work,
            )
            handlers._media_batch_buffers.pop(key, None)
            buffer = None

        if buffer is None:
            buffer = _MediaBatchBuffer(
                topic_key=topic_key,
                messages=[message],
                placeholder_id=placeholder_id,
                media_group_id=message.media_group_id,
                created_at=time.monotonic(),
            )
            handlers._media_batch_buffers[key] = buffer
        else:
            buffer.messages.append(message)
            if placeholder_id is not None:
                if buffer.placeholder_id is None:
                    buffer.placeholder_id = placeholder_id
                else:
                    drop_placeholder = True

        handlers._touch_cache_timestamp("media_batch_buffers", key)
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        window_seconds = handlers._config.media.batch_window_seconds
        buffer.task = handlers._spawn_task(
            flush_media_batch_after(handlers, key, window_seconds)
        )
    if drop_placeholder and placeholder_id is not None:
        await handlers._delete_message(message.chat_id, placeholder_id)


async def flush_media_batch_after(
    handlers: Any, key: str, window_seconds: float
) -> None:
    try:
        await asyncio.sleep(window_seconds)
    except asyncio.CancelledError:
        return
    try:
        await flush_media_batch_key(handlers, key)
    except Exception as exc:
        log_event(
            handlers._logger,
            logging.WARNING,
            "telegram.media_batch.flush_failed",
            key=key,
            exc=exc,
        )


async def flush_media_batch_key(handlers: Any, key: str) -> None:
    lock = handlers._media_batch_locks.get(key)
    if lock is None:
        return
    buffer = None
    async with lock:
        buffer = handlers._media_batch_buffers.pop(key, None)
        if buffer is None:
            return
        task = buffer.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        handlers._media_batch_locks.pop(key, None)
    if buffer.messages:

        async def work() -> None:
            await handlers._handle_media_batch(
                buffer.messages, placeholder_id=buffer.placeholder_id
            )

        first_message = buffer.messages[0]
        await _enqueue_or_run_topic_work(
            handlers,
            buffer.topic_key,
            chat_id=first_message.chat_id,
            thread_id=first_message.thread_id,
            placeholder_id=buffer.placeholder_id,
            work=work,
        )


async def handle_media_message(
    handlers: Any,
    message: TelegramMessage,
    runtime: Any,
    caption_text: str,
    *,
    placeholder_id: Optional[int] = None,
) -> None:
    if not handlers._config.media.enabled:
        await handlers._send_message(
            message.chat_id,
            "Media handling is disabled.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    key = await handlers._resolve_topic_key(message.chat_id, message.thread_id)
    record = await handlers._router.get_topic(key)
    record, pma_error = _record_with_media_workspace(handlers, record)
    if pma_error:
        await handlers._send_message(
            message.chat_id,
            pma_error,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return
    if record is None or not record.workspace_path:
        await handlers._send_message(
            message.chat_id,
            handlers._with_conversation_id(
                TOPIC_NOT_BOUND_MESSAGE,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        return

    pma_enabled = bool(getattr(record, "pma_enabled", False))
    workspace_root = canonicalize_path(Path(record.workspace_path))
    notification_reply = None
    notification_store_root = getattr(getattr(handlers, "_config", None), "root", None)
    if message.reply_to_message_id is not None and isinstance(
        notification_store_root, (str, Path)
    ):
        notification_reply = PmaNotificationStore(
            Path(notification_store_root)
        ).get_reply_target(
            surface_kind="telegram",
            surface_key=key,
            delivered_message_id=message.reply_to_message_id,
        )
    turn_caption_text = format_forwarded_telegram_message_text(message, caption_text)
    paused = None
    if not pma_enabled and notification_reply is None:
        preferred_run_id = handlers._ticket_flow_pause_targets.get(
            str(workspace_root), None
        )
        paused = handlers._get_paused_ticket_flow(
            workspace_root, preferred_run_id=preferred_run_id
        )
    event_logger = _event_logger(handlers)
    ingress = build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: log_event(
            event_logger,
            logging.INFO,
            f"telegram.{orchestration_event.event_type}",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )

    async def _resolve_paused_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> Optional[PausedFlowTarget]:
        if paused is None:
            return None
        run_id, run_record = paused
        return PausedFlowTarget(
            flow_target=FlowTarget(
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                display_name="ticket_flow",
                workspace_root=str(workspace_root),
            ),
            run_id=run_id,
            status=_paused_flow_status(run_record),
            workspace_root=workspace_root,
        )

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, flow_target: PausedFlowTarget
    ) -> None:
        if paused is None:
            return
        run_id, run_record = paused
        reply_text = caption_text.strip() if isinstance(caption_text, str) else ""
        if not reply_text:
            reply_text = "Media reply attached."
        files = []
        expected_media = bool(
            message.photos or message.document or message.audio or message.voice
        )
        if message.photos:
            photos = sorted(
                message.photos,
                key=lambda p: (p.file_size or 0, p.width * p.height),
                reverse=True,
            )
            if photos:
                best = photos[0]
                try:
                    file_info = await handlers._bot.get_file(best.file_id)
                    data = await handlers._bot.download_file(
                        file_info.file_path,
                        max_size_bytes=handlers._config.media.max_image_bytes,
                    )
                    filename = f"photo_{best.file_id}.jpg"
                    files.append((filename, data))
                except Exception as exc:
                    handlers._logger.debug("Failed to download photo: %s", exc)
        elif message.document:
            try:
                file_info = await handlers._bot.get_file(message.document.file_id)
                data = await handlers._bot.download_file(
                    file_info.file_path,
                    max_size_bytes=handlers._config.media.max_file_bytes,
                )
                filename = (
                    message.document.file_name or f"document_{message.document.file_id}"
                )
                files.append((filename, data))
            except Exception as exc:
                handlers._logger.debug("Failed to download document: %s", exc)
        elif message.audio:
            try:
                file_info = await handlers._bot.get_file(message.audio.file_id)
                data = await handlers._bot.download_file(
                    file_info.file_path,
                    max_size_bytes=handlers._config.media.max_file_bytes,
                )
                filename = message.audio.file_name or f"audio_{message.audio.file_id}"
                files.append((filename, data))
            except Exception as exc:
                handlers._logger.debug("Failed to download audio: %s", exc)
        elif message.voice:
            try:
                file_info = await handlers._bot.get_file(message.voice.file_id)
                data = await handlers._bot.download_file(
                    file_info.file_path,
                    max_size_bytes=handlers._config.media.max_voice_bytes,
                )
                files.append((f"voice_{message.voice.file_id}.ogg", data))
            except Exception as exc:
                handlers._logger.debug("Failed to download voice: %s", exc)
        if expected_media and not files:
            await handlers._send_message(
                message.chat_id,
                "Failed to download media for paused flow reply. Please retry.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        success, result = await handlers._write_user_reply_from_telegram(
            workspace_root,
            run_id,
            run_record,
            message,
            compose_forwarded_message_text(
                reply_text,
                message_forward_info(message, reply_text),
            ),
            files,
        )
        await handlers._send_message(
            message.chat_id,
            result,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        if success:
            await handlers._ticket_flow_bridge.auto_resume_run(
                workspace_root, flow_target.run_id
            )

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> None:
        image_candidate = select_image_candidate(message)
        if image_candidate:
            if not handlers._config.media.images:
                await handlers._send_message(
                    message.chat_id,
                    "Image handling is disabled.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await handlers._handle_image_message(
                message,
                runtime,
                record,
                image_candidate,
                turn_caption_text,
                placeholder_id=placeholder_id,
            )
            return

        voice_candidate = select_voice_candidate(message)
        if voice_candidate:
            if not handlers._config.media.voice:
                await handlers._send_message(
                    message.chat_id,
                    "Voice transcription is disabled.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await handlers._handle_voice_message(
                message,
                runtime,
                record,
                voice_candidate,
                turn_caption_text,
                placeholder_id=placeholder_id,
            )
            return

        file_candidate = select_file_candidate(message)
        if file_candidate:
            if not handlers._config.media.files:
                await handlers._send_message(
                    message.chat_id,
                    "File handling is disabled.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await handlers._handle_file_message(
                message,
                runtime,
                record,
                file_candidate,
                turn_caption_text,
                placeholder_id=placeholder_id,
            )
            return

        if turn_caption_text:
            await handlers._handle_normal_message(
                message,
                runtime,
                text_override=turn_caption_text,
                record=(
                    dataclasses.replace(
                        record,
                        pma_enabled=True,
                        repo_id=notification_reply.repo_id or record.repo_id,
                        workspace_path=(
                            notification_reply.workspace_root
                            or str(getattr(handlers, "_hub_root", None) or "")
                            or record.workspace_path
                        ),
                    )
                    if notification_reply is not None
                    else record
                ),
                placeholder_id=placeholder_id,
                surface_key_override=(
                    notification_surface_key(notification_reply.notification_id)
                    if notification_reply is not None
                    else None
                ),
                pma_context_prefix=(
                    build_notification_context_block(notification_reply)
                    if notification_reply is not None
                    else None
                ),
            )
            return
        await handlers._send_message(
            message.chat_id,
            "Unsupported media type.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    if notification_reply is not None:
        await _submit_thread_message(
            SurfaceThreadMessageRequest(
                surface_kind="telegram",
                workspace_root=workspace_root,
                prompt_text=turn_caption_text,
                agent_id=getattr(record, "agent", None),
                pma_enabled=True,
            )
        )
        orch_binding = _build_telegram_thread_orchestration_service(
            handlers
        ).get_binding(
            surface_kind="telegram",
            surface_key=notification_surface_key(notification_reply.notification_id),
        )
        if orch_binding is not None:
            PmaNotificationStore(handlers._config.root).bind_continuation_thread(
                notification_id=notification_reply.notification_id,
                thread_target_id=orch_binding.thread_target_id,
            )
        return
    await ingress.submit_message(
        SurfaceThreadMessageRequest(
            surface_kind="telegram",
            workspace_root=workspace_root,
            prompt_text=turn_caption_text,
            agent_id=getattr(record, "agent", None),
            pma_enabled=pma_enabled,
        ),
        resolve_paused_flow_target=_resolve_paused_flow,
        submit_flow_reply=_submit_flow_reply,
        submit_thread_message=_submit_thread_message,
    )
