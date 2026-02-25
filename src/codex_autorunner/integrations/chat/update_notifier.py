from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event

TERMINAL_UPDATE_STATES = frozenset({"ok", "error", "rollback"})
RUNNING_UPDATE_STATES = frozenset({"running", "spawned"})

_NotifySender = Callable[[dict[str, Any], str], Awaitable[None]]
_ReadUpdateStatus = Callable[[], Optional[dict[str, Any]]]
_MarkUpdateNotified = Callable[[dict[str, Any]], None]
_FormatUpdateStatus = Callable[[Optional[dict[str, Any]]], str]
_SpawnTask = Callable[[Awaitable[None]], Any]


def _is_valid_notify_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and bool(value.strip())


def _coerce_notify_context(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    chat_id = raw.get("chat_id")
    if not _is_valid_notify_id(chat_id):
        return None
    context: dict[str, Any] = {"chat_id": chat_id}
    thread_id = raw.get("thread_id")
    if _is_valid_notify_id(thread_id):
        context["thread_id"] = thread_id
    reply_to = raw.get("reply_to")
    if _is_valid_notify_id(reply_to):
        context["reply_to"] = reply_to
    return context


def _normalize_platform(platform: str) -> str:
    return str(platform or "").strip().lower()


def build_update_notify_metadata(
    *,
    platform: str,
    chat_id: int | str,
    thread_id: Optional[int | str] = None,
    reply_to: Optional[int | str] = None,
    include_legacy_telegram_keys: bool = False,
) -> dict[str, Any]:
    platform_key = _normalize_platform(platform)
    context: dict[str, Any] = {"chat_id": chat_id}
    if thread_id is not None:
        context["thread_id"] = thread_id
    if reply_to is not None:
        context["reply_to"] = reply_to
    coerced = _coerce_notify_context(context)
    if not platform_key or coerced is None:
        raise ValueError("Invalid update notification metadata.")

    payload: dict[str, Any] = {
        "notify_platform": platform_key,
        "notify_context": coerced,
    }
    if include_legacy_telegram_keys and platform_key == "telegram":
        chat_raw = coerced.get("chat_id")
        if isinstance(chat_raw, int) and not isinstance(chat_raw, bool):
            payload["notify_chat_id"] = chat_raw
        thread_raw = coerced.get("thread_id")
        if isinstance(thread_raw, int) and not isinstance(thread_raw, bool):
            payload["notify_thread_id"] = thread_raw
        reply_raw = coerced.get("reply_to")
        if isinstance(reply_raw, int) and not isinstance(reply_raw, bool):
            payload["notify_reply_to"] = reply_raw
    return payload


def extract_update_notify_context(
    status: Optional[dict[str, Any]],
    *,
    platform: str,
) -> Optional[dict[str, Any]]:
    if not isinstance(status, dict):
        return None
    platform_key = _normalize_platform(platform)
    notify_platform = status.get("notify_platform")
    notify_context = status.get("notify_context")
    if (
        isinstance(notify_platform, str)
        and _normalize_platform(notify_platform) == platform_key
        and isinstance(notify_context, dict)
    ):
        return _coerce_notify_context(notify_context)

    if platform_key != "telegram":
        return None
    legacy_chat_id = status.get("notify_chat_id")
    if not isinstance(legacy_chat_id, int) or isinstance(legacy_chat_id, bool):
        return None
    context: dict[str, Any] = {"chat_id": legacy_chat_id}
    legacy_thread_id = status.get("notify_thread_id")
    if isinstance(legacy_thread_id, int) and not isinstance(legacy_thread_id, bool):
        context["thread_id"] = legacy_thread_id
    legacy_reply_to = status.get("notify_reply_to")
    if isinstance(legacy_reply_to, int) and not isinstance(legacy_reply_to, bool):
        context["reply_to"] = legacy_reply_to
    return context


def format_update_status_message(status: Optional[dict[str, Any]]) -> str:
    if not status:
        return "No update status recorded."
    state = str(status.get("status") or "unknown")
    message = str(status.get("message") or "")
    timestamp = status.get("at")
    rendered_time = ""
    if isinstance(timestamp, (int, float)):
        rendered_time = datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    lines = [f"Update status: {state}"]
    if message:
        lines.append(f"Message: {message}")
    if rendered_time:
        lines.append(f"Last updated: {rendered_time}")
    return "\n".join(lines)


def mark_update_status_notified(
    *,
    path: Path,
    status: dict[str, Any],
    logger: logging.Logger,
    log_event_name: str,
) -> None:
    updated = dict(status)
    updated["notify_sent_at"] = time.time()
    try:
        path.write_text(json.dumps(updated), encoding="utf-8")
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            log_event_name,
            exc=exc,
        )


class ChatUpdateStatusNotifier:
    def __init__(
        self,
        *,
        platform: str,
        logger: logging.Logger,
        read_status: _ReadUpdateStatus,
        send_notice: _NotifySender,
        spawn_task: _SpawnTask,
        mark_notified: _MarkUpdateNotified,
        format_status: Optional[_FormatUpdateStatus] = None,
        running_message: str = "Update still running.",
    ) -> None:
        self._platform = _normalize_platform(platform)
        self._logger = logger
        self._read_status = read_status
        self._send_notice = send_notice
        self._spawn_task = spawn_task
        self._mark_notified = mark_notified
        self._format_status = format_status or format_update_status_message
        self._running_message = running_message

    def build_spawn_metadata(
        self,
        *,
        chat_id: int | str,
        thread_id: Optional[int | str] = None,
        reply_to: Optional[int | str] = None,
        include_legacy_telegram_keys: bool = False,
    ) -> dict[str, Any]:
        return build_update_notify_metadata(
            platform=self._platform,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
            include_legacy_telegram_keys=include_legacy_telegram_keys,
        )

    def schedule_watch(
        self,
        notify_context: dict[str, Any],
        *,
        timeout_seconds: float = 300.0,
        interval_seconds: float = 2.0,
    ) -> None:
        context = _coerce_notify_context(notify_context)
        if context is None:
            return

        async def _watch() -> None:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                status = self._read_status()
                if isinstance(status, dict):
                    state = str(status.get("status") or "")
                    if state in TERMINAL_UPDATE_STATES:
                        if status.get("notify_sent_at"):
                            return
                        resolved = (
                            extract_update_notify_context(
                                status, platform=self._platform
                            )
                            or context
                        )
                        await self._send_notice(
                            resolved,
                            self._format_status(status),
                        )
                        self._mark_notified(status)
                        return
                await asyncio.sleep(interval_seconds)
            await self._send_notice(context, self._running_message)

        self._spawn_task(_watch())

    async def maybe_send_notice(self) -> None:
        status = self._read_status()
        if not isinstance(status, dict):
            return
        context = extract_update_notify_context(status, platform=self._platform)
        if context is None:
            return
        if status.get("notify_sent_at"):
            return
        state = str(status.get("status") or "")
        if state in RUNNING_UPDATE_STATES:
            self.schedule_watch(context)
            return
        if state not in TERMINAL_UPDATE_STATES:
            return
        await self._send_notice(context, self._format_status(status))
        self._mark_notified(status)
