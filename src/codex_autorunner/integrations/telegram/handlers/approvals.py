from __future__ import annotations

import asyncio
import logging
from typing import Any

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ...app_server.client import ApprovalDecision
from ...chat.handlers.approvals import ChatApprovalHandlers
from ...chat.handlers.models import ChatContext
from ...chat.models import ChatInteractionEvent, ChatInteractionRef, ChatMessageRef
from ..adapter import ApprovalCallback, TelegramCallbackQuery, build_approval_keyboard
from ..config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ..helpers import (
    _approval_age_seconds,
    _coerce_id,
    _extract_turn_thread_id,
    _format_approval_decision,
    _format_approval_prompt,
)
from ..state import PendingApprovalRecord
from ..types import PendingApproval


class TelegramApprovalHandlers(ChatApprovalHandlers):
    async def _restore_pending_approvals(self) -> None:
        state = await self._store.load()
        if not state.pending_approvals:
            return
        grouped: dict[tuple[int, int | None], list[PendingApprovalRecord]] = {}
        for record in state.pending_approvals.values():
            key = (record.chat_id, record.thread_id)
            grouped.setdefault(key, []).append(record)
        for (chat_id, thread_id), records in grouped.items():
            items = []
            for record in records:
                age = _approval_age_seconds(record.created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                items.append(f"{record.request_id} ({age_label})")
                await self._store.clear_pending_approval(record.request_id)
            message = (
                "Cleared stale approval requests from a previous session. "
                "Re-run the request or use /interrupt if the turn is still active.\n"
                f"Requests: {', '.join(items)}"
            )
            try:
                await self._send_message(
                    chat_id,
                    message,
                    thread_id=thread_id,
                )
            except Exception:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.approval.restore_failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                )

    async def _handle_approval_request(
        self, message: dict[str, Any]
    ) -> ApprovalDecision:
        req_id = message.get("id")
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        turn_id = _coerce_id(params.get("turnId")) if isinstance(params, dict) else None
        if not req_id or not turn_id:
            return "cancel"
        codex_thread_id = _extract_turn_thread_id(params)
        ctx = self._resolve_turn_context(turn_id, thread_id=codex_thread_id)
        if ctx is None:
            return "cancel"
        request_id = str(req_id)
        prompt = _format_approval_prompt(message)
        created_at = now_iso()
        approval_record = PendingApprovalRecord(
            request_id=request_id,
            turn_id=str(turn_id),
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            message_id=None,
            prompt=prompt,
            created_at=created_at,
            topic_key=ctx.topic_key,
        )
        await self._store.upsert_pending_approval(approval_record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.requested",
            request_id=request_id,
            turn_id=turn_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
        )
        try:
            keyboard = build_approval_keyboard(request_id, include_session=False)
        except ValueError:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.callback_too_long",
                request_id=request_id,
            )
            await self._store.clear_pending_approval(request_id)
            return "cancel"
        payload_text, parse_mode = self._prepare_outgoing_text(
            prompt,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            reply_to=ctx.reply_to_message_id,
            topic_key=ctx.topic_key,
            codex_thread_id=codex_thread_id,
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
                "telegram.approval.send_failed",
                request_id=request_id,
                turn_id=turn_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                exc=exc,
            )
            await self._store.clear_pending_approval(request_id)
            try:
                await self._send_message(
                    ctx.chat_id,
                    "Approval prompt failed to send; canceling approval. "
                    "Please retry or use /interrupt.",
                    thread_id=ctx.thread_id,
                    reply_to=ctx.reply_to_message_id,
                )
            except Exception:
                pass
            return "cancel"
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if isinstance(message_id, int):
            approval_record.message_id = message_id
            await self._store.upsert_pending_approval(approval_record)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        pending = PendingApproval(
            request_id=request_id,
            turn_id=str(turn_id),
            codex_thread_id=codex_thread_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            topic_key=ctx.topic_key,
            message_id=message_id if isinstance(message_id, int) else None,
            created_at=created_at,
            future=future,
        )
        self._pending_approvals[request_id] = pending
        self._touch_cache_timestamp("pending_approvals", request_id)
        runtime = self._router.runtime_for(ctx.topic_key)
        runtime.pending_request_id = request_id
        try:
            return await asyncio.wait_for(
                future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._pending_approvals.pop(request_id, None)
            await self._store.clear_pending_approval(request_id)
            runtime.pending_request_id = None
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.timeout",
                request_id=request_id,
                turn_id=turn_id,
                chat_id=ctx.chat_id,
                thread_id=ctx.thread_id,
                timeout_seconds=DEFAULT_APPROVAL_TIMEOUT_SECONDS,
            )
            if pending.message_id is not None:
                await self._edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    "Approval timed out.",
                    reply_markup={"inline_keyboard": []},
                )
            return "cancel"
        except asyncio.CancelledError:
            self._pending_approvals.pop(request_id, None)
            await self._store.clear_pending_approval(request_id)
            runtime.pending_request_id = None
            raise

    async def _handle_approval_callback(
        self, callback: TelegramCallbackQuery, parsed: ApprovalCallback
    ) -> None:
        context = await self._chat_context_from_callback(callback)
        interaction = self._chat_interaction_event_from_callback(callback)
        await self.handle_approval_interaction(context, interaction, parsed)

    async def _chat_context_from_callback(
        self, callback: TelegramCallbackQuery
    ) -> ChatContext:
        chat_id = callback.chat_id if callback.chat_id is not None else 0
        topic_key = await self._resolve_topic_key(chat_id, callback.thread_id)
        return ChatContext(
            thread=self._chat_thread_ref(chat_id, callback.thread_id),
            topic_key=topic_key,
            user_id=(
                str(callback.from_user_id)
                if callback.from_user_id is not None
                else None
            ),
        )

    def _chat_interaction_event_from_callback(
        self, callback: TelegramCallbackQuery
    ) -> ChatInteractionEvent:
        thread = self._chat_thread_ref(
            callback.chat_id if callback.chat_id is not None else 0,
            callback.thread_id,
        )
        message = None
        if callback.message_id is not None:
            message = ChatMessageRef(thread=thread, message_id=str(callback.message_id))
        return ChatInteractionEvent(
            update_id=str(callback.update_id),
            thread=thread,
            interaction=ChatInteractionRef(
                thread=thread,
                interaction_id=callback.callback_id,
            ),
            from_user_id=(
                str(callback.from_user_id)
                if callback.from_user_id is not None
                else None
            ),
            payload=callback.data,
            message=message,
        )

    async def _chat_answer_interaction(
        self, interaction: ChatInteractionEvent, text: str
    ) -> None:
        callback = TelegramCallbackQuery(
            update_id=(
                int(interaction.update_id) if interaction.update_id.isdigit() else 0
            ),
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
                if interaction.thread.thread_id
                and interaction.thread.thread_id.isdigit()
                else None
            ),
        )
        await self._answer_callback(callback, text)

    async def _chat_edit_message(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        message_id: int,
        text: str,
        clear_actions: bool,
    ) -> None:
        reply_markup = {"inline_keyboard": []} if clear_actions else None
        await self._edit_message_text(
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
        )

    @staticmethod
    def _format_approval_decision(decision: str) -> str:
        return _format_approval_decision(decision)

    @staticmethod
    def _chat_thread_ref(chat_id: int, thread_id: int | None):
        from ...chat.models import ChatThreadRef

        return ChatThreadRef(
            platform="telegram",
            chat_id=str(chat_id),
            thread_id=str(thread_id) if thread_id is not None else None,
        )
