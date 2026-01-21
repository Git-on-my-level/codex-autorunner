from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from .constants import (
    PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
    STREAM_PREVIEW_PREFIX,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THINKING_PREVIEW_MAX_LEN,
    THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS,
    TOKEN_USAGE_CACHE_LIMIT,
    TOKEN_USAGE_TURN_CACHE_LIMIT,
)
from .helpers import (
    _coerce_id,
    _extract_context_usage_percent,
    _extract_files,
    _extract_first_bold_span,
    _extract_turn_thread_id,
    _truncate_text,
    is_interrupt_status,
)
from .progress_stream import TurnProgressTracker, render_progress_text


class TelegramNotificationHandlers:
    def _cache_token_usage(
        self,
        token_usage: dict[str, Any],
        *,
        turn_id: Optional[str],
        thread_id: Optional[str],
    ) -> None:
        if not isinstance(token_usage, dict):
            return
        if isinstance(thread_id, str) and thread_id:
            self._token_usage_by_thread[thread_id] = token_usage
            self._token_usage_by_thread.move_to_end(thread_id)
            while len(self._token_usage_by_thread) > TOKEN_USAGE_CACHE_LIMIT:
                self._token_usage_by_thread.popitem(last=False)
        if isinstance(turn_id, str) and turn_id:
            self._token_usage_by_turn[turn_id] = token_usage
            self._token_usage_by_turn.move_to_end(turn_id)
            while len(self._token_usage_by_turn) > TOKEN_USAGE_TURN_CACHE_LIMIT:
                self._token_usage_by_turn.popitem(last=False)

    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params_raw = message.get("params")
        params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
        if method == "car/app_server/oversizedMessageDropped":
            turn_id = _coerce_id(params.get("turnId"))
            thread_id = params.get("threadId")
            turn_key = (
                self._resolve_turn_key(turn_id, thread_id=thread_id)
                if turn_id
                else None
            )
            if turn_key is None and len(self._turn_contexts) == 1:
                turn_key = next(iter(self._turn_contexts.keys()))
            if turn_key is None:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.oversize.context_missing",
                    inferred_turn_id=turn_id,
                    inferred_thread_id=thread_id,
                )
                return
            if turn_key in self._oversize_warnings:
                return
            ctx = self._turn_contexts.get(turn_key)
            if ctx is None:
                return
            self._oversize_warnings.add(turn_key)
            self._touch_cache_timestamp("oversize_warnings", turn_key)
            byte_limit = params.get("byteLimit")
            limit_mb = None
            if isinstance(byte_limit, int) and byte_limit > 0:
                limit_mb = max(1, byte_limit // (1024 * 1024))
            limit_text = f"{limit_mb}MB" if limit_mb else "the size limit"
            aborted = bool(params.get("aborted"))
            inferred_method = params.get("inferredMethod")
            inferred_thread_id = _coerce_id(params.get("threadId"))
            inferred_turn_id = _coerce_id(params.get("turnId"))
            if aborted:
                warning = (
                    f"Warning: Codex output exceeded {limit_text} and kept growing. "
                    "CAR is dropping output until a newline to recover. Avoid huge "
                    "stdout (use head/tail, filters, or redirect to a file)."
                )
            else:
                warning = (
                    f"Warning: Codex output exceeded {limit_text} and was dropped to "
                    "keep the session alive. Avoid huge stdout (use head/tail, "
                    "filters, or redirect to a file)."
                )
            context_parts = []
            if isinstance(inferred_method, str) and inferred_method:
                context_parts.append(f"method={inferred_method}")
            if isinstance(inferred_thread_id, str) and inferred_thread_id:
                context_parts.append(f"thread={inferred_thread_id}")
            if isinstance(inferred_turn_id, str) and inferred_turn_id:
                context_parts.append(f"turn={inferred_turn_id}")
            if context_parts:
                warning = f"{warning} Context: {', '.join(context_parts)}."
            if len(warning) > TELEGRAM_MAX_MESSAGE_LENGTH:
                warning = warning[: TELEGRAM_MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
            await self._send_message_with_outbox(
                ctx.chat_id,
                warning,
                thread_id=ctx.thread_id,
                reply_to=ctx.reply_to_message_id,
                placeholder_id=ctx.placeholder_message_id,
            )
            return
        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = _coerce_id(params.get("turnId"))
            token_usage = params.get("tokenUsage")
            if not isinstance(thread_id, str) or not isinstance(token_usage, dict):
                return
            self._cache_token_usage(token_usage, turn_id=turn_id, thread_id=thread_id)
            if self._config.progress_stream.enabled:
                await self._note_progress_context_usage(
                    token_usage, turn_id=turn_id, thread_id=thread_id
                )
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
                if self._config.progress_stream.enabled:
                    await self._note_progress_thinking(
                        turn_id, preview, thread_id=thread_id
                    )
                else:
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
            if isinstance(item, dict) and item.get("type") == "reasoning":
                item_id = _coerce_id(item.get("id") or params.get("itemId"))
                if item_id:
                    self._reasoning_buffers.pop(item_id, None)
            if self._config.progress_stream.enabled:
                await self._note_progress_item_completed(params)
            return
        if self._config.progress_stream.enabled:
            if method in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                await self._note_progress_approval(method, params)
                return
            if method == "turn/completed":
                await self._note_progress_turn_completed(params)
                return
            if method == "error":
                await self._note_progress_error(params)
                return
            if isinstance(method, str) and "outputDelta" in method:
                await self._note_progress_output_delta(params)
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

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Any,
        agent: str,
        model: Optional[str],
        label: str = "working",
    ) -> None:
        if not self._config.progress_stream.enabled:
            return
        tracker = TurnProgressTracker(
            started_at=time.monotonic(),
            agent=agent,
            model=model or "default",
            label=label,
            max_actions=self._config.progress_stream.max_actions,
            max_output_chars=self._config.progress_stream.max_output_chars,
        )
        self._turn_progress_trackers[turn_key] = tracker
        self._turn_progress_rendered.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)
        self._touch_cache_timestamp("progress_trackers", turn_key)
        pending_context_usage: dict[tuple[str, str], int] = getattr(
            self, "_pending_context_usage", {}
        )
        if pending_context_usage:
            pending_value = pending_context_usage.pop(turn_key, None)
            if pending_value is not None:
                tracker.set_context_usage_percent(pending_value)
        if ctx:
            chat_id = ctx.chat_id
            thread_id = ctx.thread_id
        else:
            chat_id = None
            thread_id = None
        log_event(
            self._logger,
            logging.INFO,
            "telegram.progress.first",
            topic_key=ctx.topic_key if ctx else None,
            chat_id=chat_id,
            thread_id=thread_id,
            first_progress_at=now_iso(),
        )
        await self._emit_progress_edit(turn_key, ctx=ctx, force=True)
        heartbeat_task = self._turn_progress_heartbeat_tasks.get(turn_key)
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        self._turn_progress_heartbeat_tasks[turn_key] = self._spawn_task(
            self._turn_progress_heartbeat(turn_key)
        )

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        self._turn_progress_trackers.pop(turn_key, None)
        self._turn_progress_rendered.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)
        self._turn_progress_locks.pop(turn_key, None)
        pending_context_usage: dict[tuple[str, str], int] = getattr(
            self, "_pending_context_usage", {}
        )
        if pending_context_usage:
            pending_context_usage.pop(turn_key, None)
        task = self._turn_progress_tasks.pop(turn_key, None)
        if task and not task.done():
            task.cancel()
        heartbeat_task = self._turn_progress_heartbeat_tasks.pop(turn_key, None)
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()

    async def _note_progress_thinking(
        self, turn_id: str, preview: str, *, thread_id: Optional[str] = None
    ) -> None:
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        tracker.note_thinking(preview)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_context_usage(
        self,
        token_usage: dict[str, Any],
        *,
        turn_id: Optional[str],
        thread_id: Optional[str],
    ) -> None:
        percent = _extract_context_usage_percent(token_usage)
        if percent is None:
            return
        turn_key = None
        if turn_id:
            turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None and len(self._turn_contexts) == 1:
            turn_key = next(iter(self._turn_contexts.keys()))
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            pending_context_usage: dict[tuple[str, str], int] = getattr(
                self, "_pending_context_usage", None
            )
            if pending_context_usage is None:
                pending_context_usage = {}
                self._pending_context_usage = pending_context_usage
            pending_context_usage[turn_key] = percent
            return
        tracker.set_context_usage_percent(percent)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        turn_id = _coerce_id(params.get("turnId") or item.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        item_type = item.get("type")
        if item_type == "reasoning":
            return
        if item_type == "commandExecution":
            command = _extract_command_text(item, params)
            if command:
                tracker.note_command(command)
        elif item_type == "fileChange":
            files = _extract_files(item)
            summary = ", ".join(files) if files else "Updated files"
            tracker.note_file_change(summary)
        elif item_type == "tool":
            tool = item.get("name") or item.get("tool") or item.get("id") or "Tool call"
            tracker.note_tool(str(tool))
        elif item_type == "agentMessage":
            text = item.get("text") or "Agent message"
            tracker.add_action("agent", str(text), "done")
        else:
            text = item.get("text") or item.get("message") or "Item completed"
            tracker.add_action("item", str(text), "done")
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_approval(
        self, method: str, params: dict[str, Any]
    ) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        if method == "item/commandExecution/requestApproval":
            summary = (
                _extract_command_text(None, params) or "Command approval requested"
            )
        elif method == "item/fileChange/requestApproval":
            files = _extract_files(params)
            summary = ", ".join(files) if files else "File approval requested"
        else:
            summary = "Approval requested"
        tracker.note_approval(summary)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_output_delta(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        delta = params.get("delta") or params.get("text")
        if not isinstance(delta, str):
            return
        tracker.note_output(delta)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_error(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        message = _extract_error_message(params)
        tracker.note_error(message or "App-server error")
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_turn_completed(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        status = params.get("status")
        if isinstance(status, str) and is_interrupt_status(status):
            tracker.set_label("cancelled")
        elif isinstance(status, str) and status and status != "completed":
            tracker.set_label("failed")
        else:
            tracker.set_label("done")
        tracker.finalized = True
        await self._emit_progress_edit(turn_key, force=True)
        self._clear_turn_progress(turn_key)

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        lock = self._turn_progress_locks.setdefault(turn_key, asyncio.Lock())
        async with lock:
            tracker = self._turn_progress_trackers.get(turn_key)
            ctx = self._turn_contexts.get(turn_key)
            if tracker is None or ctx is None or ctx.placeholder_message_id is None:
                return
            if tracker.finalized:
                return
            min_interval = self._config.progress_stream.min_edit_interval_seconds
            now = time.monotonic()
            last_updated = self._turn_progress_updated_at.get(turn_key, 0.0)
            if (now - last_updated) >= min_interval:
                await self._emit_progress_edit(turn_key, ctx=ctx, now=now)
                return
            if turn_key in self._turn_progress_tasks:
                return
            delay = max(min_interval - (now - last_updated), 0.0)
            task = self._spawn_task(self._delayed_progress_edit(turn_key, delay))
            self._turn_progress_tasks[turn_key] = task

    async def _delayed_progress_edit(
        self, turn_key: tuple[str, str], delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._emit_progress_edit(turn_key)
        finally:
            self._turn_progress_tasks.pop(turn_key, None)

    async def _turn_progress_heartbeat(self, turn_key: tuple[str, str]) -> None:
        try:
            while True:
                await asyncio.sleep(PROGRESS_HEARTBEAT_INTERVAL_SECONDS)
                tracker = self._turn_progress_trackers.get(turn_key)
                if tracker is None or tracker.finalized:
                    return
                ctx = self._turn_contexts.get(turn_key)
                if ctx is None or ctx.placeholder_message_id is None:
                    continue
                now = time.monotonic()
                last_updated = self._turn_progress_updated_at.get(turn_key, 0.0)
                if (now - last_updated) >= PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
                    await self._emit_progress_edit(turn_key, ctx=ctx, now=now)
        finally:
            self._turn_progress_heartbeat_tasks.pop(turn_key, None)

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
    ) -> None:
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        if ctx is None:
            ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        if now is None:
            now = time.monotonic()
        rendered = render_progress_text(
            tracker, max_length=TELEGRAM_MAX_MESSAGE_LENGTH, now=now
        )
        if not force and rendered == self._turn_progress_rendered.get(turn_key):
            return
        reply_markup = None
        if tracker.label in {"working", "queued", "running"}:
            try:
                reply_markup = self._interrupt_keyboard()
            except Exception:
                reply_markup = None
        ok = await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            rendered,
            reply_markup=reply_markup,
        )
        if ok:
            self._turn_progress_rendered[turn_key] = rendered
            self._turn_progress_updated_at[turn_key] = now
            self._touch_cache_timestamp("progress_trackers", turn_key)


def _extract_command_text(
    item: Optional[dict[str, Any]], params: dict[str, Any]
) -> str:
    command = None
    if isinstance(item, dict):
        command = item.get("command")
    if command is None:
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""


def _extract_error_message(params: dict[str, Any]) -> str:
    err = params.get("error")
    if isinstance(err, dict):
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        details = ""
        if isinstance(err.get("additionalDetails"), str):
            details = err["additionalDetails"]
        elif isinstance(err.get("details"), str):
            details = err["details"]
        if message and details and message != details:
            return f"{message} ({details})"
        return message or details
    if isinstance(err, str):
        return err
    if isinstance(params.get("message"), str):
        return params["message"]
    return ""
