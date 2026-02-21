from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence, Union

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ...chat.handlers.models import ChatContext
from ...chat.handlers.questions import (
    ChatQuestionHandlers,
)
from ...chat.handlers.questions import (
    handle_custom_text_input as handle_custom_text_input_chat,
)
from ...chat.models import (
    ChatInteractionEvent,
    ChatInteractionRef,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from ..adapter import (
    QuestionCancelCallback,
    QuestionCustomCallback,
    QuestionDoneCallback,
    QuestionOptionCallback,
    TelegramCallbackQuery,
    TelegramMessage,
    build_question_keyboard,
)
from ..config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ..types import PendingQuestion


def _extract_question_text(question: dict[str, Any]) -> str:
    for key in ("text", "prompt", "title", "label", "question"):
        value = question.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Question"


def _extract_question_options(question: dict[str, Any]) -> tuple[list[str], bool, bool]:
    multiple = bool(question.get("multiple"))
    custom = question.get("custom", True)
    for key in ("options", "choices"):
        raw = question.get(key)
        if isinstance(raw, list):
            options: list[str] = []
            for option in raw:
                if isinstance(option, str) and option.strip():
                    options.append(option.strip())
                    continue
                if isinstance(option, dict):
                    for label_key in ("label", "text", "value", "name", "id"):
                        value = option.get(label_key)
                        if isinstance(value, str) and value.strip():
                            options.append(value.strip())
                            break
            return options, multiple, custom
    return [], multiple, custom


def _format_question_prompt(question: dict[str, Any], *, index: int, total: int) -> str:
    title = _extract_question_text(question)
    if total > 1:
        prefix = f"Question {index + 1} of {total}"
    else:
        prefix = "Question"
    return f"{prefix}:\n{title}"


async def handle_custom_text_input(handlers: Any, message: TelegramMessage) -> bool:
    event = _chat_message_event_from_telegram(message)
    return await handle_custom_text_input_chat(handlers, event)


class TelegramQuestionHandlers(ChatQuestionHandlers):
    async def _handle_question_request(
        self,
        *,
        request_id: str,
        turn_id: str,
        thread_id: str,
        questions: Sequence[dict[str, Any]],
    ) -> list[list[str]] | None:
        if not request_id or not turn_id:
            return None
        ctx = self._resolve_turn_context(turn_id, thread_id=thread_id)
        if ctx is None:
            return None
        if not questions:
            return None
        answers: list[list[str]] = []
        for index, question in enumerate(questions):
            options, multiple, custom = _extract_question_options(question)
            if not options:
                answers.append([])
                continue
            prompt = _format_question_prompt(
                question, index=index, total=len(questions)
            )
            try:
                keyboard = build_question_keyboard(
                    request_id,
                    question_index=index,
                    options=options,
                    multiple=multiple,
                    custom=custom,
                )
            except ValueError:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.question.callback_too_long",
                    request_id=request_id,
                    question_index=index,
                )
                await self._send_message(
                    ctx.chat_id,
                    "Question prompt too long to send; answering as unanswered.",
                    thread_id=ctx.thread_id,
                    reply_to=ctx.reply_to_message_id,
                )
                answers.append([])
                continue
            payload_text, parse_mode = self._prepare_outgoing_text(
                prompt,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                reply_to=ctx.reply_to_message_id,
                topic_key=ctx.topic_key,
                codex_thread_id=ctx.codex_thread_id,
            )
            try:
                response = await self._bot.send_message(
                    ctx.chat_id,
                    payload_text,
                    message_thread_id=ctx.thread_id,
                    reply_to_message_id=ctx.reply_to_message_id,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.question.send_failed",
                    request_id=request_id,
                    question_index=index,
                    chat_id=ctx.chat_id,
                    thread_id=ctx.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    ctx.chat_id,
                    "Question prompt failed to send; rejecting question.",
                    thread_id=ctx.thread_id,
                    reply_to=ctx.reply_to_message_id,
                )
                return None
            message_id = (
                response.get("message_id") if isinstance(response, dict) else None
            )
            created_at = now_iso()
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Union[list[int], str, None]] = loop.create_future()
            pending = PendingQuestion(
                request_id=request_id,
                turn_id=str(turn_id),
                codex_thread_id=thread_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                topic_key=ctx.topic_key,
                message_id=message_id if isinstance(message_id, int) else None,
                created_at=created_at,
                question_index=index,
                prompt=prompt,
                options=options,
                future=future,
                multiple=multiple,
                custom=custom,
                selected_indices=set(),
                awaiting_custom_input=False,
            )
            self._pending_questions[request_id] = pending
            self._touch_cache_timestamp("pending_questions", request_id)
            try:
                result = await asyncio.wait_for(
                    future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                self._pending_questions.pop(request_id, None)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.question.timeout",
                    request_id=request_id,
                    question_index=index,
                    chat_id=ctx.chat_id,
                    thread_id=ctx.thread_id,
                    timeout_seconds=DEFAULT_APPROVAL_TIMEOUT_SECONDS,
                )
                if pending.message_id is not None:
                    await self._edit_message_text(
                        pending.chat_id,
                        pending.message_id,
                        "Question timed out.",
                        reply_markup={"inline_keyboard": []},
                    )
                return None
            except asyncio.CancelledError:
                self._pending_questions.pop(request_id, None)
                raise
            if result is None:
                return None
            if isinstance(result, str):
                answers.append([result])
            else:
                selected_options = [options[i] for i in result if 0 <= i < len(options)]
                answers.append(selected_options)
        return answers

    async def _handle_question_callback(
        self,
        callback: TelegramCallbackQuery,
        parsed: (
            QuestionOptionCallback
            | QuestionDoneCallback
            | QuestionCustomCallback
            | QuestionCancelCallback
        ),
    ) -> None:
        context = await self._chat_context_from_callback(callback)
        interaction = _chat_interaction_event_from_telegram(callback)
        await self.handle_question_interaction(context, interaction, parsed)

    async def _chat_context_from_callback(
        self, callback: TelegramCallbackQuery
    ) -> ChatContext:
        chat_id = callback.chat_id if callback.chat_id is not None else 0
        topic_key = await self._resolve_topic_key(chat_id, callback.thread_id)
        return ChatContext(
            thread=ChatThreadRef(
                platform="telegram",
                chat_id=str(chat_id),
                thread_id=(
                    str(callback.thread_id) if callback.thread_id is not None else None
                ),
            ),
            topic_key=topic_key,
            user_id=(
                str(callback.from_user_id)
                if callback.from_user_id is not None
                else None
            ),
        )

    async def _chat_answer_interaction(
        self, interaction: ChatInteractionEvent, text: str
    ) -> None:
        callback = _telegram_callback_from_chat(interaction)
        await self._answer_callback(callback, text)

    async def _chat_edit_message(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        message_id: int,
        text: str,
        clear_actions: bool,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        if clear_actions:
            reply_markup = {"inline_keyboard": []}
        await self._edit_message_text(
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
        )

    async def _chat_send_message(
        self, *, chat_id: int, thread_id: int | None, text: str
    ) -> None:
        await self._send_message(chat_id, text, thread_id=thread_id)

    async def _chat_delete_message(
        self, *, chat_id: str, thread_id: str | None, message_id: str
    ) -> None:
        if not chat_id.isdigit() or not message_id.isdigit():
            return
        parsed_thread = int(thread_id) if thread_id and thread_id.isdigit() else None
        await self._delete_message(int(chat_id), int(message_id), parsed_thread)

    @staticmethod
    def _build_question_keyboard(
        request_id: str,
        *,
        question_index: int,
        options: list[str],
        multiple: bool = False,
        custom: bool = True,
        selected_indices: set[int] | None = None,
    ) -> dict[str, Any]:
        return build_question_keyboard(
            request_id,
            question_index=question_index,
            options=options,
            multiple=multiple,
            custom=custom,
            selected_indices=selected_indices,
            include_cancel=True,
        )


def _chat_interaction_event_from_telegram(
    callback: TelegramCallbackQuery,
) -> ChatInteractionEvent:
    thread = ChatThreadRef(
        platform="telegram",
        chat_id=str(callback.chat_id) if callback.chat_id is not None else "0",
        thread_id=str(callback.thread_id) if callback.thread_id is not None else None,
    )
    message_ref = None
    if callback.message_id is not None:
        message_ref = ChatMessageRef(thread=thread, message_id=str(callback.message_id))
    return ChatInteractionEvent(
        update_id=str(callback.update_id),
        thread=thread,
        interaction=ChatInteractionRef(
            thread=thread,
            interaction_id=callback.callback_id,
        ),
        from_user_id=(
            str(callback.from_user_id) if callback.from_user_id is not None else None
        ),
        payload=callback.data,
        message=message_ref,
    )


def _telegram_callback_from_chat(
    interaction: ChatInteractionEvent,
) -> TelegramCallbackQuery:
    return TelegramCallbackQuery(
        update_id=int(interaction.update_id) if interaction.update_id.isdigit() else 0,
        callback_id=interaction.interaction.interaction_id,
        from_user_id=(
            int(interaction.from_user_id)
            if interaction.from_user_id and interaction.from_user_id.isdigit()
            else None
        ),
        data=interaction.payload,
        message_id=(
            int(interaction.message.message_id)
            if interaction.message and interaction.message.message_id.isdigit()
            else None
        ),
        chat_id=(
            int(interaction.thread.chat_id)
            if interaction.thread.chat_id.isdigit()
            else None
        ),
        thread_id=(
            int(interaction.thread.thread_id)
            if interaction.thread.thread_id and interaction.thread.thread_id.isdigit()
            else None
        ),
    )


def _chat_message_event_from_telegram(message: TelegramMessage) -> ChatMessageEvent:
    thread = ChatThreadRef(
        platform="telegram",
        chat_id=str(message.chat_id),
        thread_id=str(message.thread_id) if message.thread_id is not None else None,
    )
    return ChatMessageEvent(
        update_id=str(message.update_id),
        thread=thread,
        message=ChatMessageRef(thread=thread, message_id=str(message.message_id)),
        from_user_id=(
            str(message.from_user_id) if message.from_user_id is not None else None
        ),
        text=message.text,
        is_edited=message.is_edited,
    )
