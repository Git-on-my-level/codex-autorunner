"""Chat-core approval interaction handling.

This module is platform-agnostic and depends only on normalized chat models.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ....core.logging_utils import log_event
from ..models import ChatInteractionEvent
from .models import ChatContext


class ChatApprovalHandlers:
    """Approval callback behavior over normalized chat interaction events."""

    _chat_answer_interaction: Any
    _store: Any
    _pending_approvals: dict[str, Any]

    def _resolve_turn_context(
        self, turn_id: str, thread_id: Optional[str] = None
    ) -> Optional[Any]:
        """Resolve turn context by ID."""
        raise NotImplementedError

    @property
    def _router(self) -> Any:
        """Get the router."""
        raise NotImplementedError

    _logger: logging.Logger

    async def _chat_edit_message(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        message_id: str,
        text: str,
        reply_markup: Any = None,
        clear_actions: bool = False,
    ) -> None:
        """Edit a chat message."""
        raise NotImplementedError

    def _format_approval_decision(self, decision: str) -> str:
        """Format an approval decision for display."""
        raise NotImplementedError

    async def handle_approval_interaction(
        self,
        context: ChatContext,
        interaction: ChatInteractionEvent,
        parsed: Any,
    ) -> None:
        request_id = getattr(parsed, "request_id", None)
        decision = getattr(parsed, "decision", None)
        if not isinstance(request_id, str) or not isinstance(decision, str):
            await self._chat_answer_interaction(interaction, "Approval already handled")
            return

        await self._store.clear_pending_approval(request_id)
        pending = self._pending_approvals.pop(request_id, None)
        if pending is None:
            await self._chat_answer_interaction(interaction, "Approval already handled")
            return
        if not pending.future.done():
            pending.future.set_result(decision)
        ctx = self._resolve_turn_context(
            pending.turn_id, thread_id=pending.codex_thread_id
        )
        if ctx:
            runtime_key = ctx.topic_key
        elif pending.topic_key:
            runtime_key = pending.topic_key
        else:
            runtime_key = context.topic_key
        runtime = self._router.runtime_for(runtime_key)
        runtime.pending_request_id = None
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.decision",
            request_id=request_id,
            decision=decision,
            chat_id=context.thread.chat_id,
            thread_id=context.thread.thread_id,
            message_id=interaction.message.message_id if interaction.message else None,
        )
        await self._chat_answer_interaction(interaction, f"Decision: {decision}")
        if pending.message_id is not None:
            try:
                await self._chat_edit_message(
                    chat_id=pending.chat_id,
                    thread_id=pending.thread_id,
                    message_id=pending.message_id,
                    text=self._format_approval_decision(decision),
                    clear_actions=True,
                )
            except Exception:
                return
