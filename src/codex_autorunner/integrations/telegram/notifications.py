from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ...core.logging_utils import log_event
from .constants import (
    PLACEHOLDER_TEXT,
    STREAM_PREVIEW_PREFIX,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THINKING_PREVIEW_MAX_LEN,
    THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS,
    TOKEN_USAGE_CACHE_LIMIT,
    TOKEN_USAGE_TURN_CACHE_LIMIT,
    TURN_PROGRESS_MAX_LEN,
    TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
    TurnKey,
)
from .helpers import (
    _coerce_id,
    _extract_command_text,
    _extract_files,
    _extract_first_bold_span,
    _extract_turn_thread_id,
    _truncate_text,
)


class TelegramNotificationHandlers:
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
            if aborted:
                warning = (
                    f"Warning: Codex output exceeded {limit_text} and kept growing, "
                    "so CAR restarted the app-server to recover. Avoid huge stdout "
                    "(use head/tail, filters, or redirect to a file)."
                )
            else:
                warning = (
                    f"Warning: Codex output exceeded {limit_text} and was dropped to "
                    "keep the session alive. Avoid huge stdout (use head/tail, "
                    "filters, or redirect to a file)."
                )
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
            if isinstance(item, dict) and item.get("type") == "reasoning":
                item_id = _coerce_id(item.get("id") or params.get("itemId"))
                if item_id:
                    self._reasoning_buffers.pop(item_id, None)
                return
        progress = self._format_turn_progress(message, params)
        if progress:
            turn_id = self._extract_turn_id(message, params)
            thread_id = _extract_turn_thread_id(params)
            if turn_id:
                await self._update_placeholder_progress(
                    turn_id,
                    progress,
                    thread_id=thread_id,
                )
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
        message_text = self._render_placeholder_message(turn_key)
        if not message_text:
            return
        await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            message_text,
        )

    def _render_placeholder_message(self, turn_key: "TurnKey") -> Optional[str]:
        progress = self._turn_progress_text.get(turn_key)
        preview = self._turn_preview_text.get(turn_key)
        lines = [PLACEHOLDER_TEXT]
        if progress:
            lines.append(progress)
        if preview:
            prefix = STREAM_PREVIEW_PREFIX.strip()
            if prefix:
                lines.append(f"{prefix} {preview}".strip())
            else:
                lines.append(f"Thinking: {preview}")
        message_text = "\n".join(line for line in lines if line).strip()
        if not message_text:
            return None
        if len(message_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            message_text = _truncate_text(message_text, TELEGRAM_MAX_MESSAGE_LENGTH)
        return message_text

    def _extract_turn_id(
        self, message: dict[str, Any], params: dict[str, Any]
    ) -> Optional[str]:
        for key in ("turnId", "turn_id", "id"):
            value = _coerce_id(params.get(key))
            if value:
                return value
        for candidate in (params.get("turn"), params.get("item")):
            if isinstance(candidate, dict):
                for key in ("id", "turnId", "turn_id"):
                    value = _coerce_id(candidate.get(key))
                    if value:
                        return value
        for key in ("turnId", "turn_id", "id"):
            value = _coerce_id(message.get(key))
            if value:
                return value
        return None

    def _format_turn_progress(
        self, message: dict[str, Any], params: dict[str, Any]
    ) -> Optional[str]:
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return None
        method_lower = method.lower()
        if "outputdelta" in method_lower:
            return None
        if method in (
            "item/reasoning/summaryTextDelta",
            "item/reasoning/summaryPartAdded",
        ):
            return None
        item = params.get("item")
        item = item if isinstance(item, dict) else {}
        if method == "item/commandExecution/requestApproval":
            command = _extract_command_text(item, params)
            return f"Approval: {command}" if command else "Approval required"
        if method == "item/fileChange/requestApproval":
            files = _extract_files(params)
            summary = self._format_file_list(files)
            return f"Approval: {summary}" if summary else "Approval required"
        if method == "item/completed":
            item_type = item.get("type")
            if item_type == "reasoning":
                return None
            if item_type == "commandExecution":
                command = _extract_command_text(item, params)
                exit_code = item.get("exitCode")
                suffix = ""
                if isinstance(exit_code, int) and exit_code != 0:
                    suffix = f" (exit {exit_code})"
                if command:
                    return f"Command: {command}{suffix}"
                return f"Command completed{suffix}"
            if item_type == "fileChange":
                files = _extract_files(item) or _extract_files(params)
                summary = self._format_file_list(files)
                return f"Files: {summary}" if summary else "Files updated"
            if item_type == "tool":
                tool = item.get("name") or item.get("tool") or item.get("id")
                if isinstance(tool, str) and tool.strip():
                    return f"Tool: {tool.strip()}"
                return "Tool completed"
            if item_type == "agentMessage":
                text = item.get("text") or item.get("message")
                if isinstance(text, str) and text.strip():
                    normalized = " ".join(text.split())
                    return f"Agent: {normalized}"
                return "Agent message"
            text = item.get("text") or item.get("message")
            if isinstance(text, str) and text.strip():
                normalized = " ".join(text.split())
                return f"Update: {normalized}"
            return f"{item_type} completed" if item_type else "Item completed"
        if method == "turn/completed":
            status = params.get("status")
            if isinstance(status, str) and status.strip():
                return f"Turn: {status.strip()}"
            return "Turn completed"
        if method == "error":
            summary = self._format_error_summary(params)
            return f"Error: {summary}" if summary else "Error"
        return None

    def _format_file_list(self, files: list[str], *, limit: int = 3) -> str:
        cleaned = [
            path.strip() for path in files if isinstance(path, str) and path.strip()
        ]
        if not cleaned:
            return ""
        if len(cleaned) <= limit:
            return ", ".join(cleaned)
        remaining = len(cleaned) - limit
        return f"{', '.join(cleaned[:limit])} +{remaining} more"

    def _format_error_summary(self, params: dict[str, Any]) -> str:
        err = params.get("error") if isinstance(params, dict) else None
        if isinstance(err, dict):
            message = err.get("message")
            details = err.get("additionalDetails") or err.get("details")
            message_text = message.strip() if isinstance(message, str) else ""
            details_text = details.strip() if isinstance(details, str) else ""
            if message_text and details_text and message_text != details_text:
                return f"{message_text} ({details_text})"
            return message_text or details_text
        if isinstance(err, str):
            return err.strip()
        raw_message = params.get("message") if isinstance(params, dict) else None
        if isinstance(raw_message, str):
            return raw_message.strip()
        return ""

    async def _update_placeholder_progress(
        self, turn_id: str, text: str, *, thread_id: Optional[str] = None
    ) -> None:
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        normalized = " ".join(text.split()).strip()
        if not normalized:
            return
        normalized = _truncate_text(normalized, TURN_PROGRESS_MAX_LEN)
        if normalized == self._turn_progress_text.get(turn_key):
            return
        now = time.monotonic()
        last_updated = self._turn_progress_updated_at.get(turn_key, 0.0)
        if (now - last_updated) < TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS:
            return
        self._turn_progress_text[turn_key] = normalized
        self._turn_progress_updated_at[turn_key] = now
        self._touch_cache_timestamp("turn_progress", turn_key)
        message_text = self._render_placeholder_message(turn_key)
        if not message_text:
            return
        await self._edit_message_text(
            ctx.chat_id,
            ctx.placeholder_message_id,
            message_text,
        )
