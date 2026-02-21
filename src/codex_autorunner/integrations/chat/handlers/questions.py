"""Chat-core question handling over normalized events."""

from __future__ import annotations

import logging
from typing import Any

from ....core.logging_utils import log_event
from ..models import ChatInteractionEvent, ChatMessageEvent
from .models import ChatContext


async def handle_custom_text_input(handlers: Any, event: ChatMessageEvent) -> bool:
    """Handle custom question answers from normalized message events."""

    text = event.text or ""
    if not text:
        return False
    for request_id, pending in list(handlers._pending_questions.items()):
        if (
            pending.awaiting_custom_input
            and str(pending.chat_id) == event.thread.chat_id
            and (
                pending.thread_id is None
                or str(pending.thread_id) == (event.thread.thread_id or "")
            )
        ):
            handlers._pending_questions.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result(text)
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.question.custom_input",
                request_id=request_id,
                chat_id=event.thread.chat_id,
                thread_id=event.thread.thread_id,
                text_length=len(text),
            )
            if pending.message_id is not None:
                await handlers._chat_edit_message(
                    chat_id=pending.chat_id,
                    thread_id=pending.thread_id,
                    message_id=pending.message_id,
                    text=f"Selected: {text}",
                    clear_actions=True,
                )
            await handlers._chat_delete_message(
                chat_id=event.thread.chat_id,
                thread_id=event.thread.thread_id,
                message_id=event.message.message_id,
            )
            return True
    return False


class ChatQuestionHandlers:
    """Question callback handling over normalized interaction events."""

    async def handle_question_interaction(
        self,
        context: ChatContext,
        interaction: ChatInteractionEvent,
        parsed: Any,
    ) -> None:
        request_id = getattr(parsed, "request_id", None)
        if not isinstance(request_id, str):
            await self._chat_answer_interaction(interaction, "Selection expired")
            return
        pending = self._pending_questions.get(request_id)
        if pending is None:
            await self._chat_answer_interaction(interaction, "Selection expired")
            return
        event_message_id = (
            interaction.message.message_id if interaction.message else None
        )
        if (
            pending.message_id is not None
            and str(pending.message_id) != event_message_id
        ):
            await self._chat_answer_interaction(interaction, "Selection expired")
            return

        parsed_type = type(parsed).__name__
        if parsed_type == "QuestionCancelCallback":
            self._pending_questions.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result(None)
            log_event(
                self._logger,
                logging.INFO,
                "telegram.question.cancelled",
                request_id=request_id,
                chat_id=context.thread.chat_id,
                thread_id=context.thread.thread_id,
            )
            await self._chat_answer_interaction(interaction, "Canceled")
            if pending.message_id is not None:
                await self._chat_edit_message(
                    chat_id=pending.chat_id,
                    thread_id=pending.thread_id,
                    message_id=pending.message_id,
                    text="Question canceled.",
                    clear_actions=True,
                )
            return

        if parsed_type == "QuestionCustomCallback":
            if not pending.custom:
                await self._chat_answer_interaction(
                    interaction, "Custom input disabled"
                )
                return
            pending.awaiting_custom_input = True
            await self._chat_answer_interaction(interaction, "Enter your answer below")
            await self._chat_send_message(
                chat_id=pending.chat_id,
                thread_id=pending.thread_id,
                text="Please type your custom answer:",
            )
            return

        if parsed_type == "QuestionDoneCallback":
            if not pending.multiple:
                await self._chat_answer_interaction(
                    interaction, "Invalid for single-select"
                )
                return
            if not pending.selected_indices:
                await self._chat_answer_interaction(interaction, "No selections")
                return
            self._pending_questions.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result(list(pending.selected_indices))
            selections = ", ".join(pending.options[i] for i in pending.selected_indices)
            log_event(
                self._logger,
                logging.INFO,
                "telegram.question.done",
                request_id=request_id,
                question_index=pending.question_index,
                selections=selections,
                chat_id=context.thread.chat_id,
                thread_id=context.thread.thread_id,
            )
            await self._chat_answer_interaction(interaction, "Done")
            if pending.message_id is not None:
                await self._chat_edit_message(
                    chat_id=pending.chat_id,
                    thread_id=pending.thread_id,
                    message_id=pending.message_id,
                    text=f"Selected: {selections}",
                    clear_actions=True,
                )
            return

        if parsed_type != "QuestionOptionCallback":
            await self._chat_answer_interaction(interaction, "Selection expired")
            return

        question_index = getattr(parsed, "question_index", None)
        option_index = getattr(parsed, "option_index", None)
        if question_index != pending.question_index:
            await self._chat_answer_interaction(interaction, "Selection expired")
            return
        if (
            not isinstance(option_index, int)
            or option_index < 0
            or option_index >= len(pending.options)
        ):
            await self._chat_answer_interaction(interaction, "Invalid selection")
            return
        if not pending.multiple:
            self._pending_questions.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_result([option_index])
            log_event(
                self._logger,
                logging.INFO,
                "telegram.question.selected",
                request_id=request_id,
                question_index=question_index,
                option_index=option_index,
                chat_id=context.thread.chat_id,
                thread_id=context.thread.thread_id,
            )
            await self._chat_answer_interaction(interaction, "Selected")
            if pending.message_id is not None:
                await self._chat_edit_message(
                    chat_id=pending.chat_id,
                    thread_id=pending.thread_id,
                    message_id=pending.message_id,
                    text=f"Selected: {pending.options[option_index]}",
                    clear_actions=True,
                )
            return

        if option_index in pending.selected_indices:
            pending.selected_indices.remove(option_index)
            display_msg = "Deselected"
        else:
            pending.selected_indices.add(option_index)
            display_msg = "Selected"
        updated_keyboard = self._build_question_keyboard(
            request_id,
            question_index=pending.question_index,
            options=pending.options,
            multiple=True,
            custom=pending.custom,
            selected_indices=pending.selected_indices,
        )
        await self._chat_answer_interaction(interaction, display_msg)
        if pending.message_id is not None:
            await self._chat_edit_message(
                chat_id=pending.chat_id,
                thread_id=pending.thread_id,
                message_id=pending.message_id,
                text=pending.prompt,
                reply_markup=updated_keyboard,
                clear_actions=False,
            )
