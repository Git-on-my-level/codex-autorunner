from __future__ import annotations

import time
from typing import Any, Optional

from .constants import (
    STREAM_PREVIEW_PREFIX,
    THINKING_PREVIEW_MAX_LEN,
    THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS,
    TOKEN_USAGE_CACHE_LIMIT,
    TOKEN_USAGE_TURN_CACHE_LIMIT,
)
from .helpers import (
    _coerce_id,
    _extract_first_bold_span,
    _extract_turn_thread_id,
    _truncate_text,
)


class TelegramNotificationHandlers:
    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params_raw = message.get("params")
        params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = _coerce_id(params.get("turnId"))
            token_usage = params.get("tokenUsage")
            if not isinstance(thread_id, str) or not isinstance(token_usage, dict):
                return
            self._token_usage_by_thread[thread_id] = token_usage
            self._token_usage_by_thread.move_to_end(thread_id)
            while len(self._token_usage_by_thread) > TOKEN_USAGE_CACHE_LIMIT:
                self._token_usage_by_thread.popitem(last=False)
            if turn_id:
                self._token_usage_by_turn[turn_id] = token_usage
                self._token_usage_by_turn.move_to_end(turn_id)
                while len(self._token_usage_by_turn) > TOKEN_USAGE_TURN_CACHE_LIMIT:
                    self._token_usage_by_turn.popitem(last=False)
            return
        if method == "item/reasoning/summaryTextDelta":
            item_id = _coerce_id(params.get("itemId"))
            turn_id = _coerce_id(params.get("turnId"))
            thread_id = _extract_turn_thread_id(params)
            delta = params.get("delta")
            if not item_id or not turn_id or not isinstance(delta, str):
                return
            buffer = self._reasoning_buffers.get(item_id, "")
            buffer = f"{buffer}{delta}"
            self._reasoning_buffers[item_id] = buffer
            self._touch_cache_timestamp("reasoning_buffers", item_id)
            preview = _extract_first_bold_span(buffer)
            if preview:
                await self._update_placeholder_preview(
                    turn_id, preview, thread_id=thread_id
                )
            return
        if method == "item/reasoning/summaryPartAdded":
            item_id = _coerce_id(params.get("itemId"))
            if not item_id:
                return
            buffer = self._reasoning_buffers.get(item_id, "")
            buffer = f"{buffer}\n\n"
            self._reasoning_buffers[item_id] = buffer
            self._touch_cache_timestamp("reasoning_buffers", item_id)
            return
        if method == "item/completed":
            item = params.get("item") if isinstance(params, dict) else None
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                return
            item_id = _coerce_id(item.get("id") or params.get("itemId"))
            if item_id:
                self._reasoning_buffers.pop(item_id, None)
            return

    async def _update_placeholder_preview(
        self, turn_id: str, preview: str, *, thread_id: Optional[str] = None
    ) -> None:
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        normalized = " ".join(preview.split()).strip()
        if not normalized:
            return
        normalized = _truncate_text(normalized, THINKING_PREVIEW_MAX_LEN)
        if normalized == self._turn_preview_text.get(turn_key):
            return
        now = time.monotonic()
        last_updated = self._turn_preview_updated_at.get(turn_key, 0.0)
        if (now - last_updated) < THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS:
            return
        self._turn_preview_text[turn_key] = normalized
        self._turn_preview_updated_at[turn_key] = now
        self._touch_cache_timestamp("turn_preview", turn_key)
        if STREAM_PREVIEW_PREFIX:
            message_text = f"{STREAM_PREVIEW_PREFIX} {normalized}"
        else:
            message_text = normalized
        await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            message_text,
        )
