from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    TokenUsage,
)
from ...core.state import now_iso
from ...core.text_delta_coalescer import TextDeltaCoalescer
from ..chat.agents import DEFAULT_CHAT_AGENT_MODELS
from ..chat.managed_thread_progress import progress_item_id_for_log_line
from ..chat.managed_thread_progress_projector import (
    ManagedThreadProgressProjector,
)
from .constants import (
    PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
    PROGRESS_HEARTBEAT_MAX_SECONDS,
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

_ACTIVE_PROGRESS_LABELS = {"working", "queued", "running", "review"}
_DEGRADED_PROGRESS_MARKERS = (
    "reconnecting",
    "stalled",
    "stall timeout",
    "session idle",
)
_DEGRADED_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 30.0
_PROGRESS_EDIT_FAILURE_INITIAL_BACKOFF_SECONDS = 15.0
_PROGRESS_EDIT_FAILURE_MAX_BACKOFF_SECONDS = 120.0


def _turn_log_fields(turn_key: tuple[str, str]) -> dict[str, str]:
    return {
        "backend_thread_id": turn_key[0],
        "turn_id": turn_key[1],
    }


class TelegramNotificationHandlers:
    def _ensure_turn_progress_projectors(
        self,
    ) -> dict[tuple[str, str], ManagedThreadProgressProjector]:
        projectors = getattr(self, "_turn_progress_projectors", None)
        if isinstance(projectors, dict):
            return projectors
        projectors = {}
        self._turn_progress_projectors = projectors
        return projectors

    def _get_turn_progress_projector(
        self,
        turn_key: tuple[str, str],
        *,
        create: bool = False,
    ) -> Optional[ManagedThreadProgressProjector]:
        projectors = TelegramNotificationHandlers._ensure_turn_progress_projectors(self)
        projector = projectors.get(turn_key)
        if projector is not None or not create:
            return projector
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return None
        progress_config = getattr(self._config, "progress_stream", None)
        min_interval = (
            getattr(progress_config, "min_edit_interval_seconds", 0.0)
            if progress_config is not None
            else 0.0
        )
        projector = ManagedThreadProgressProjector(
            tracker,
            min_render_interval_seconds=min_interval,
            heartbeat_interval_seconds=PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
            initial_phase="queued" if tracker.label == "queued" else "working",
        )
        projectors[turn_key] = projector
        return projector

    def _render_turn_progress_text(
        self,
        turn_key: tuple[str, str],
        *,
        render_mode: str,
        now: Optional[float] = None,
    ) -> str:
        projector = TelegramNotificationHandlers._get_turn_progress_projector(
            self,
            turn_key,
            create=False,
        )
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return ""
        if projector is not None:
            return projector.render(
                max_length=TELEGRAM_MAX_MESSAGE_LENGTH,
                now=now,
                render_mode=render_mode,
            )
        return render_progress_text(
            tracker,
            max_length=TELEGRAM_MAX_MESSAGE_LENGTH,
            now=time.monotonic() if now is None else now,
            render_mode=render_mode,
        )

    def _progress_tracker_is_degraded(
        self,
        turn_key: tuple[str, str],
        tracker: Any,
    ) -> bool:
        texts: list[str] = []
        for candidate in (
            getattr(tracker, "transient_action", None),
            getattr(tracker, "last_tool_trace", None),
            getattr(tracker, "last_thinking_trace", None),
        ):
            text = getattr(candidate, "text", None)
            if isinstance(text, str) and text.strip():
                texts.append(text)
        latest_output = getattr(tracker, "latest_output_text", None)
        if callable(latest_output):
            value = latest_output()
            if isinstance(value, str) and value.strip():
                texts.append(value)
        rendered = self._turn_progress_rendered.get(turn_key)
        if isinstance(rendered, str) and rendered.strip():
            texts.append(rendered)
        if not texts:
            return False
        normalized = " ".join(" ".join(texts).lower().split())
        return any(marker in normalized for marker in _DEGRADED_PROGRESS_MARKERS)

    def _progress_edit_min_interval(
        self,
        turn_key: tuple[str, str],
        tracker: Any,
    ) -> float:
        min_interval = self._config.progress_stream.min_edit_interval_seconds
        if TelegramNotificationHandlers._progress_tracker_is_degraded(
            self, turn_key, tracker
        ):
            min_interval = max(
                min_interval,
                _DEGRADED_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
            )
        blocked_until = getattr(self, "_turn_progress_backoff_until", {}).get(
            turn_key, 0.0
        )
        now = time.monotonic()
        if blocked_until > now:
            min_interval = max(min_interval, blocked_until - now)
        return min_interval

    def _record_progress_edit_suppressed(self, turn_key: tuple[str, str]) -> None:
        suppressed = getattr(self, "_turn_progress_suppressed_counts", None)
        if not isinstance(suppressed, dict):
            suppressed = {}
            self._turn_progress_suppressed_counts = suppressed
        suppressed[turn_key] = int(suppressed.get(turn_key, 0)) + 1

    def _record_progress_edit_failure(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any],
        now: float,
        degraded: bool,
    ) -> None:
        failure_streaks = getattr(self, "_turn_progress_failure_streaks", None)
        if not isinstance(failure_streaks, dict):
            failure_streaks = {}
            self._turn_progress_failure_streaks = failure_streaks
        streak = int(failure_streaks.get(turn_key, 0)) + 1
        failure_streaks[turn_key] = streak

        backoff_until = getattr(self, "_turn_progress_backoff_until", None)
        if not isinstance(backoff_until, dict):
            backoff_until = {}
            self._turn_progress_backoff_until = backoff_until
        base_delay = max(
            self._config.progress_stream.min_edit_interval_seconds,
            _PROGRESS_EDIT_FAILURE_INITIAL_BACKOFF_SECONDS,
        )
        if degraded:
            base_delay = max(
                base_delay,
                _DEGRADED_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
            )
        delay = min(
            base_delay * (2 ** (streak - 1)),
            _PROGRESS_EDIT_FAILURE_MAX_BACKOFF_SECONDS,
        )
        backoff_until[turn_key] = now + delay
        log_event(
            self._logger,
            logging.INFO,
            "telegram.progress.edit_backoff",
            chat_id=getattr(ctx, "chat_id", None),
            thread_id=getattr(ctx, "thread_id", None),
            topic_key=getattr(ctx, "topic_key", None),
            **_turn_log_fields(turn_key),
            delay_seconds=round(delay, 3),
            failure_streak=streak,
            degraded=degraded,
        )

    def _clear_progress_edit_failure_state(
        self,
        turn_key: tuple[str, str],
        *,
        log_recovery: bool = True,
    ) -> None:
        backoff_until = getattr(self, "_turn_progress_backoff_until", None)
        if isinstance(backoff_until, dict):
            backoff_until.pop(turn_key, None)
        failure_streaks = getattr(self, "_turn_progress_failure_streaks", None)
        if isinstance(failure_streaks, dict):
            streak = int(failure_streaks.pop(turn_key, 0))
        else:
            streak = 0
        suppressed = getattr(self, "_turn_progress_suppressed_counts", None)
        if isinstance(suppressed, dict):
            suppressed_count = int(suppressed.pop(turn_key, 0))
        else:
            suppressed_count = 0
        if log_recovery and (streak > 0 or suppressed_count > 0):
            log_event(
                self._logger,
                logging.INFO,
                "telegram.progress.edit_recovered",
                **_turn_log_fields(turn_key),
                failure_streak=streak,
                suppressed_edits=suppressed_count,
            )

    def _render_turn_progress_summary(self, turn_key: tuple[str, str]) -> str:
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            cached_map = getattr(self, "_turn_progress_final_summary", None)
            if not isinstance(cached_map, dict):
                return ""
            cached = cached_map.pop(turn_key, "")
            return cached if isinstance(cached, str) else ""
        rendered = TelegramNotificationHandlers._render_turn_progress_text(
            self,
            turn_key,
            render_mode="live",
            now=time.monotonic(),
        )
        line = rendered.splitlines()[0].strip() if rendered else ""
        return line

    def _render_final_turn_progress(self, turn_key: tuple[str, str]) -> str:
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            cached_map = getattr(self, "_turn_progress_final_rendered", None)
            if not isinstance(cached_map, dict):
                return ""
            cached = cached_map.pop(turn_key, "")
            return cached if isinstance(cached, str) else ""
        return TelegramNotificationHandlers._render_turn_progress_text(
            self,
            turn_key,
            render_mode="final",
            now=time.monotonic(),
        )

    def _cache_final_turn_progress(self, turn_key: tuple[str, str]) -> None:
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        cached_map = getattr(self, "_turn_progress_final_rendered", None)
        if not isinstance(cached_map, dict):
            cached_map = {}
            self._turn_progress_final_rendered = cached_map
        rendered = TelegramNotificationHandlers._render_turn_progress_text(
            self,
            turn_key,
            render_mode="final",
            now=time.monotonic(),
        )
        if rendered.strip():
            cached_map[turn_key] = rendered
            summary_map = getattr(self, "_turn_progress_final_summary", None)
            if not isinstance(summary_map, dict):
                summary_map = {}
                self._turn_progress_final_summary = summary_map
            summary_line = self._render_turn_progress_summary(turn_key)
            if summary_line:
                summary_map[turn_key] = summary_line
            touch_cache_timestamp = getattr(self, "_touch_cache_timestamp", None)
            if callable(touch_cache_timestamp):
                touch_cache_timestamp("progress_trackers", turn_key)

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
            if item_id not in self._reasoning_buffers:
                self._reasoning_buffers[item_id] = TextDeltaCoalescer()
            self._reasoning_buffers[item_id].add(delta)
            self._touch_cache_timestamp("reasoning_buffers", item_id)
            buffer = self._reasoning_buffers[item_id].get_buffer()
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
            coalescer = self._reasoning_buffers.get(item_id)
            if coalescer:
                coalescer.add("\n\n")
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
                await self._note_progress_output_delta(method, params)
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
            model=model or DEFAULT_CHAT_AGENT_MODELS.get(agent, "default"),
            label=label,
            max_actions=self._config.progress_stream.max_actions,
            max_output_chars=self._config.progress_stream.max_output_chars,
        )
        projector = ManagedThreadProgressProjector(
            tracker,
            min_render_interval_seconds=self._config.progress_stream.min_edit_interval_seconds,
            heartbeat_interval_seconds=PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
            initial_phase="queued" if label == "queued" else "working",
        )
        self._turn_progress_trackers[turn_key] = tracker
        TelegramNotificationHandlers._ensure_turn_progress_projectors(self)[
            turn_key
        ] = projector
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
            if getattr(ctx, "placeholder_message_id", None) is not None:
                projector.bind_anchor(
                    str(ctx.placeholder_message_id),
                    owned=True,
                    reused=bool(getattr(ctx, "placeholder_reused", False)),
                )
        else:
            chat_id = None
            thread_id = None
        if label == "queued":
            projector.mark_queued(force=True)
        else:
            projector.mark_working(force=True)
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
        TelegramNotificationHandlers._ensure_turn_progress_projectors(self).pop(
            turn_key, None
        )
        self._turn_progress_rendered.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)
        self._turn_progress_locks.pop(turn_key, None)
        TelegramNotificationHandlers._clear_progress_edit_failure_state(
            self,
            turn_key,
            log_recovery=False,
        )
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
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                latest_output = tracker.latest_output_text().strip()
                incoming_output = text.strip()
                if latest_output and (
                    incoming_output == latest_output
                    or incoming_output.startswith(latest_output)
                ):
                    tracker.note_output(text)
                else:
                    tracker.note_output(text, new_segment=True)
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

    async def _note_progress_output_delta(
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
        delta = params.get("delta") or params.get("text")
        if not isinstance(delta, str):
            return
        delta_type_raw = params.get("deltaType") or params.get("delta_type")
        delta_type = delta_type_raw.strip() if isinstance(delta_type_raw, str) else ""
        has_explicit_delta_type = bool(delta_type)
        is_log_line = delta_type == RUN_EVENT_DELTA_TYPE_LOG_LINE
        if not is_log_line and not has_explicit_delta_type and method == "outputDelta":
            # Older emitters may omit deltaType; preserve in-place token usage updates.
            is_log_line = progress_item_id_for_log_line(delta) is not None
        if is_log_line:
            item_id = progress_item_id_for_log_line(delta)
            if item_id:
                if not tracker.update_action_by_item_id(
                    item_id,
                    delta,
                    "update",
                    label="output",
                ):
                    tracker.add_action(
                        "output",
                        delta,
                        "update",
                        item_id=item_id,
                        normalize_text=False,
                    )
            else:
                tracker.note_output(delta, new_segment=True)
                tracker.end_output_segment()
        else:
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
        final_text = _extract_turn_completed_final_text(params)
        if final_text:
            tracker.drop_terminal_output_if_duplicate(final_text)
        tracker.clear_transient_action()
        tracker.finalized = True
        self._cache_final_turn_progress(turn_key)
        await self._emit_progress_edit(turn_key, force=True, render_mode="final")
        self._clear_turn_progress(turn_key)

    async def _apply_run_event_to_progress(
        self,
        turn_key: tuple[str, str],
        run_event: Any,
    ) -> None:
        tracker = self._turn_progress_trackers.get(turn_key)
        projector = TelegramNotificationHandlers._get_turn_progress_projector(
            self,
            turn_key,
            create=True,
        )
        if tracker is None or projector is None:
            return

        if isinstance(run_event, TokenUsage):
            usage_payload = run_event.usage
            if isinstance(usage_payload, dict):
                projector.note_context_usage(
                    _extract_context_usage_percent(usage_payload)
                )
                await self._schedule_progress_edit(turn_key)
            return

        outcome = projector.apply_run_event(run_event)
        if not outcome.changed:
            return
        if outcome.terminal:
            tracker.finalized = True
            self._cache_final_turn_progress(turn_key)
            await self._emit_progress_edit(
                turn_key,
                force=outcome.force,
                render_mode=outcome.render_mode,
            )
            self._clear_turn_progress(turn_key)
            return
        await self._schedule_progress_edit(turn_key)

    async def _ensure_turn_progress_lock(
        self, turn_key: tuple[str, str]
    ) -> asyncio.Lock:
        lock = self._turn_progress_locks.get(turn_key)
        if lock is not None:
            return lock
        guard: Optional[asyncio.Lock] = getattr(
            self, "_turn_progress_locks_guard", None
        )
        if guard is None:
            guard = asyncio.Lock()
            self._turn_progress_locks_guard = guard
        async with guard:
            lock = self._turn_progress_locks.get(turn_key)
            if lock is None:
                lock = asyncio.Lock()
                self._turn_progress_locks[turn_key] = lock
        return lock

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        lock = await self._ensure_turn_progress_lock(turn_key)
        async with lock:
            tracker = self._turn_progress_trackers.get(turn_key)
            ctx = self._turn_contexts.get(turn_key)
            if tracker is None or ctx is None or ctx.placeholder_message_id is None:
                return
            if tracker.finalized:
                return
            now = time.monotonic()
            blocked_until = getattr(self, "_turn_progress_backoff_until", {}).get(
                turn_key, 0.0
            )
            if blocked_until > now:
                TelegramNotificationHandlers._record_progress_edit_suppressed(
                    self,
                    turn_key,
                )
                if turn_key in self._turn_progress_tasks:
                    return
                delay = max(blocked_until - now, 0.0)
                task = self._spawn_task(self._delayed_progress_edit(turn_key, delay))
                self._turn_progress_tasks[turn_key] = task
                return
            min_interval = TelegramNotificationHandlers._progress_edit_min_interval(
                self,
                turn_key,
                tracker,
            )
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
        heartbeat_started_at = time.monotonic()
        try:
            while True:
                await asyncio.sleep(PROGRESS_HEARTBEAT_INTERVAL_SECONDS)
                now = time.monotonic()
                if (
                    PROGRESS_HEARTBEAT_MAX_SECONDS > 0
                    and (now - heartbeat_started_at) >= PROGRESS_HEARTBEAT_MAX_SECONDS
                ):
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.progress.heartbeat_expired",
                        **_turn_log_fields(turn_key),
                        max_seconds=PROGRESS_HEARTBEAT_MAX_SECONDS,
                    )
                    return
                tracker = self._turn_progress_trackers.get(turn_key)
                projector = TelegramNotificationHandlers._get_turn_progress_projector(
                    self,
                    turn_key,
                    create=True,
                )
                if tracker is None or projector is None or tracker.finalized:
                    return
                if not projector.should_emit_heartbeat(now=now):
                    continue
                ctx = self._turn_contexts.get(turn_key)
                if ctx is None or ctx.placeholder_message_id is None:
                    continue
                await self._schedule_progress_edit(turn_key)
        finally:
            self._turn_progress_heartbeat_tasks.pop(turn_key, None)

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
        render_mode: str = "live",
    ) -> None:
        tracker = self._turn_progress_trackers.get(turn_key)
        projector = TelegramNotificationHandlers._get_turn_progress_projector(
            self,
            turn_key,
            create=True,
        )
        if tracker is None or projector is None:
            return
        if ctx is None:
            ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        if now is None:
            now = time.monotonic()
        blocked_until = getattr(self, "_turn_progress_backoff_until", {}).get(
            turn_key, 0.0
        )
        if not force and blocked_until > now:
            TelegramNotificationHandlers._record_progress_edit_suppressed(
                self,
                turn_key,
            )
            return
        degraded = TelegramNotificationHandlers._progress_tracker_is_degraded(
            self,
            turn_key,
            tracker,
        )
        rendered = projector.render(
            max_length=TELEGRAM_MAX_MESSAGE_LENGTH,
            now=now,
            render_mode=render_mode,
        )
        if not projector.should_emit_render(
            rendered,
            now=now,
            force=force,
            min_interval_seconds=(
                None
                if force
                else TelegramNotificationHandlers._progress_edit_min_interval(
                    self,
                    turn_key,
                    tracker,
                )
            ),
        ):
            return
        reply_markup: Optional[dict[str, Any]] = {"inline_keyboard": []}
        if tracker.label in _ACTIVE_PROGRESS_LABELS:
            try:
                reply_markup = self._interrupt_keyboard()
            except (
                Exception
            ):  # intentional: non-critical keyboard builder, any failure is safe to ignore
                self._logger.debug(
                    "Telegram interrupt keyboard build failed", exc_info=True
                )
                reply_markup = None
        edit_result = await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            rendered,
            reply_markup=reply_markup,
        )
        ok = edit_result is not False
        if ok:
            TelegramNotificationHandlers._clear_progress_edit_failure_state(
                self,
                turn_key,
            )
            projector.note_rendered(rendered, now=now)
            self._turn_progress_rendered[turn_key] = rendered
            self._turn_progress_updated_at[turn_key] = now
            self._touch_cache_timestamp("progress_trackers", turn_key)
            return
        TelegramNotificationHandlers._record_progress_edit_failure(
            self,
            turn_key,
            ctx=ctx,
            now=now,
            degraded=degraded,
        )


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


def _extract_turn_completed_final_text(params: dict[str, Any]) -> str:
    direct_keys = (
        "finalMessage",
        "final_message",
        "message",
        "output",
        "text",
    )
    for key in direct_keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    result = params.get("result")
    if isinstance(result, dict):
        for key in direct_keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""
