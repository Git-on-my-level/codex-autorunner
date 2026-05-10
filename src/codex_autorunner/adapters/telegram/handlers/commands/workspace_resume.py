from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

import httpx

from .....core.logging_utils import log_event
from .....core.state import now_iso
from ....app_server import is_missing_thread_error
from ....app_server.client import CodexAppServerClient, CodexAppServerError
from ....chat.constants import (
    APP_SERVER_UNAVAILABLE_MESSAGE,
    TOPIC_NOT_BOUND_MESSAGE,
    TOPIC_NOT_BOUND_RESUME_MESSAGE,
)
from ....chat.thread_summaries import _format_resume_timestamp
from ...adapter import TelegramCallbackQuery, TelegramMessage
from ...config import AppServerUnavailableError
from ...constants import (
    DEFAULT_PAGE_SIZE,
    MAX_TOPIC_THREAD_HISTORY,
    RESUME_MISSING_IDS_LOG_LIMIT,
    RESUME_PICKER_PROMPT,
    RESUME_REFRESH_LIMIT,
    THREAD_LIST_MAX_PAGES,
)
from ...helpers import (
    _coerce_thread_list,
    _extract_first_user_preview,
    _extract_thread_info,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_missing_thread_label,
    _format_resume_summary,
    _format_thread_preview,
    _local_workspace_threads,
    _page_slice,
    _partition_threads,
    _paths_compatible,
    _resume_thread_list_limit,
    _set_thread_summary,
    _split_topic_key,
    _thread_summary_preview,
    _with_conversation_id,
)
from ...types import SelectionState
from .agent_model_utils import _extract_opencode_session_path

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord


@dataclass
class ResumeCommandArgs:
    trimmed: str
    remaining: list[str]
    show_unscoped: bool
    refresh: bool


@dataclass
class ResumeThreadData:
    candidates: list[dict[str, Any]]
    entries_by_id: dict[str, dict[str, Any]]
    local_thread_ids: list[str]
    local_previews: dict[str, str]
    local_thread_topics: dict[str, set[str]]
    list_failed: bool
    threads: list[dict[str, Any]]
    unscoped_entries: list[dict[str, Any]]
    saw_path: bool


class WorkspaceResumeMixin:
    async def _handle_opencode_resume(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        *,
        key: str,
        show_unscoped: bool,
        refresh: bool,
    ) -> None:
        if refresh:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.opencode.resume.refresh_ignored",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        store_state = None
        if show_unscoped:
            store_state = await self._store.load()
            (
                local_thread_ids,
                local_previews,
                local_thread_topics,
            ) = _local_workspace_threads(
                store_state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
            allowed_thread_ids: set[str] = set()
            for thread_id in local_thread_ids:
                if thread_id in record.thread_ids:
                    allowed_thread_ids.add(thread_id)
                    continue
                for topic_key in local_thread_topics.get(thread_id, set()):
                    topic_record = (
                        store_state.topics.get(topic_key) if store_state else None
                    )
                    if topic_record and topic_record.agent == "opencode":
                        allowed_thread_ids.add(thread_id)
                        break
            if allowed_thread_ids:
                local_thread_ids = [
                    thread_id
                    for thread_id in local_thread_ids
                    if thread_id in allowed_thread_ids
                ]
                local_previews = {
                    thread_id: preview
                    for thread_id, preview in local_previews.items()
                    if thread_id in allowed_thread_ids
                }
            else:
                local_thread_ids = []
                local_previews = {}
        else:
            for thread_id in record.thread_ids:
                local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        if not local_thread_ids:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No previous OpenCode threads found for this topic. "
                    "Use /new to start one.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for thread_id in local_thread_ids:
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            preview = local_previews.get(thread_id)
            label = _format_missing_thread_label(thread_id, preview)
            items.append((thread_id, label))
        state = SelectionState(
            items=items,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        parsed_args = self._parse_resume_args(args)
        if await self._handle_resume_shortcuts(key, message, parsed_args):
            return
        record = await self._router.get_topic(key)
        record = await self._ensure_resume_record(message, record, allow_pma=True)
        if record is None:
            return
        if record.pma_enabled and not parsed_args.show_unscoped:
            parsed_args = ResumeCommandArgs(
                trimmed=parsed_args.trimmed,
                remaining=parsed_args.remaining,
                show_unscoped=True,
                refresh=parsed_args.refresh,
            )
        if self._effective_agent(record) == "opencode":
            await self._handle_opencode_resume(
                message,
                record,
                key=key,
                show_unscoped=parsed_args.show_unscoped,
                refresh=parsed_args.refresh,
            )
            return
        client = await self._get_resume_client(message, record)
        if client is None:
            return
        thread_data = await self._gather_resume_threads(
            message,
            record,
            client,
            key=key,
            show_unscoped=parsed_args.show_unscoped,
        )
        if thread_data is None:
            return
        await self._render_resume_picker(
            message,
            record,
            key,
            parsed_args,
            thread_data,
            client,
        )

    def _parse_resume_args(self, args: str) -> ResumeCommandArgs:
        argv = self._parse_command_args(args)
        trimmed = args.strip()
        show_unscoped = False
        refresh = False
        remaining: list[str] = []
        for arg in argv:
            lowered = arg.lower()
            if lowered in ("--all", "all", "--unscoped", "unscoped"):
                show_unscoped = True
                continue
            if lowered in ("--refresh", "refresh"):
                refresh = True
                continue
            remaining.append(arg)
        if argv:
            trimmed = " ".join(remaining).strip()
        return ResumeCommandArgs(
            trimmed=trimmed,
            remaining=remaining,
            show_unscoped=show_unscoped,
            refresh=refresh,
        )

    async def _handle_resume_shortcuts(
        self, key: str, message: TelegramMessage, args: ResumeCommandArgs
    ) -> bool:
        trimmed = args.trimmed
        if trimmed.isdigit():
            state = self._resume_options.get(key)
            if state:
                page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
                choice = int(trimmed)
                if 0 < choice <= len(page_items):
                    thread_id = page_items[choice - 1][0]
                    await self._selection_resume_thread_by_id(key, thread_id)
                    return True
        if trimmed and not trimmed.isdigit():
            if args.remaining and args.remaining[0].lower() in ("list", "ls"):
                return False
            await self._selection_resume_thread_by_id(key, trimmed)
            return True
        return False

    async def _ensure_resume_record(
        self,
        message: TelegramMessage,
        record: Optional["TelegramTopicRecord"],
        *,
        allow_pma: bool = False,
    ) -> Optional["TelegramTopicRecord"]:
        if record is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if not record.workspace_path:
            if allow_pma and record.pma_enabled:
                workspace_path, error = self._resolve_workspace_path(
                    record, allow_pma=True
                )
                if workspace_path is None:
                    await self._send_message(
                        message.chat_id,
                        error or "PMA unavailable; hub root not configured.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return None
                record = self._record_with_workspace_path(record, workspace_path)
            else:
                await self._send_message(
                    message.chat_id,
                    TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        agent = self._effective_agent(record)
        if not self._agent_supports_resume(agent):
            supported_agents = set(
                self._agents_supporting_capability("durable_threads")
            )
            supported = ", ".join(sorted(supported_agents))
            agent_label = self._agent_display_name(agent)
            await self._send_message(
                message.chat_id,
                (
                    f"Resume is unavailable for {agent_label}. "
                    "The active agent must support durable threads."
                    + (f" Available agents: {supported}." if supported else "")
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return record

    async def _get_resume_client(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[CodexAppServerClient]:
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        return client

    async def _gather_resume_threads(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        client: CodexAppServerClient,
        *,
        key: str,
        show_unscoped: bool,
    ) -> Optional[ResumeThreadData]:
        if not show_unscoped and not record.thread_ids:
            await self._send_message(
                message.chat_id,
                "No previous threads found for this topic. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        threads: list[dict[str, Any]] = []
        list_failed = False
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        if show_unscoped:
            store_state = await self._store.load()
            (
                local_thread_ids,
                local_previews,
                local_thread_topics,
            ) = _local_workspace_threads(
                store_state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        limit = _resume_thread_list_limit(record.thread_ids)
        needed_ids = (
            None if show_unscoped or not record.thread_ids else set(record.thread_ids)
        )
        try:
            threads, _ = await self._list_threads_paginated(
                client,
                limit=limit,
                max_pages=THREAD_LIST_MAX_PAGES,
                needed_ids=needed_ids,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            list_failed = True
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            if show_unscoped and not local_thread_ids:
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list threads; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        entries_by_id: dict[str, dict[str, Any]] = {}
        for entry in threads:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                entries_by_id[entry_id] = entry
        candidates: list[dict[str, Any]] = []
        unscoped_entries: list[dict[str, Any]] = []
        saw_path = False
        if show_unscoped:
            if threads:
                filtered, unscoped_entries, saw_path = _partition_threads(
                    threads, record.workspace_path
                )
                seen_ids = {
                    entry.get("id")
                    for entry in filtered
                    if isinstance(entry.get("id"), str)
                }
                candidates = filtered + [
                    entry
                    for entry in unscoped_entries
                    if entry.get("id") not in seen_ids
                ]
            if not candidates and not local_thread_ids:
                if unscoped_entries and not saw_path:
                    await self._send_message(
                        message.chat_id,
                        _with_conversation_id(
                            "No workspace-tagged threads available. Use /resume --all to list "
                            "unscoped threads.",
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                        ),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return None
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "No previous threads found for this workspace. "
                        "If threads exist, update the app-server to include cwd metadata or use /new.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
        return ResumeThreadData(
            candidates=candidates,
            entries_by_id=entries_by_id,
            local_thread_ids=local_thread_ids,
            local_previews=local_previews,
            local_thread_topics=local_thread_topics,
            list_failed=list_failed,
            threads=threads,
            unscoped_entries=unscoped_entries,
            saw_path=saw_path,
        )

    async def _render_resume_picker(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        key: str,
        args: ResumeCommandArgs,
        thread_data: ResumeThreadData,
        client: CodexAppServerClient,
    ) -> None:
        entries_by_id = thread_data.entries_by_id
        local_thread_ids = thread_data.local_thread_ids
        local_previews = thread_data.local_previews
        local_thread_topics = thread_data.local_thread_topics
        missing_ids: list[str] = []
        if args.show_unscoped:
            for thread_id in local_thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        else:
            for thread_id in record.thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        if args.refresh and missing_ids:
            refreshed = await self._refresh_thread_summaries(
                client,
                missing_ids,
                topic_keys_by_thread=(
                    local_thread_topics if args.show_unscoped else None
                ),
                default_topic_key=key,
            )
            if refreshed:
                if args.show_unscoped:
                    store_state = await self._store.load()
                    (
                        local_thread_ids,
                        local_previews,
                        local_thread_topics,
                    ) = _local_workspace_threads(
                        store_state, record.workspace_path, current_key=key
                    )
                    for thread_id in record.thread_ids:
                        local_thread_topics.setdefault(thread_id, set()).add(key)
                        if thread_id not in local_thread_ids:
                            local_thread_ids.append(thread_id)
                        cached_preview = _thread_summary_preview(record, thread_id)
                        if cached_preview:
                            local_previews.setdefault(thread_id, cached_preview)
                else:
                    record = await self._router.get_topic(key) or record
        items: list[tuple[str, str]] = []
        button_labels: dict[str, str] = {}
        seen_item_ids: set[str] = set()
        if args.show_unscoped:
            for entry in thread_data.candidates:
                candidate_id = entry.get("id")
                if not isinstance(candidate_id, str) or not candidate_id:
                    continue
                if candidate_id in seen_item_ids:
                    continue
                seen_item_ids.add(candidate_id)
                label = _format_thread_preview(entry)
                button_label = _extract_first_user_preview(entry)
                timestamp = _format_resume_timestamp(entry)
                if timestamp and button_label:
                    button_labels[candidate_id] = f"{timestamp} · {button_label}"
                elif timestamp:
                    button_labels[candidate_id] = timestamp
                elif button_label:
                    button_labels[candidate_id] = button_label
                if label == "(no preview)":
                    cached_preview = local_previews.get(candidate_id)
                    if cached_preview:
                        label = cached_preview
                items.append((candidate_id, label))
            for thread_id in local_thread_ids:
                if thread_id in seen_item_ids:
                    continue
                seen_item_ids.add(thread_id)
                cached_preview = local_previews.get(thread_id)
                label = (
                    cached_preview
                    if cached_preview
                    else _format_missing_thread_label(thread_id, None)
                )
                items.append((thread_id, label))
        else:
            if record.thread_ids:
                for thread_id in record.thread_ids:
                    entry_data = entries_by_id.get(thread_id)
                    if entry_data is None:
                        cached_preview = _thread_summary_preview(record, thread_id)
                        label = _format_missing_thread_label(thread_id, cached_preview)
                    else:
                        label = _format_thread_preview(entry_data)
                        button_label = _extract_first_user_preview(entry_data)
                        timestamp = _format_resume_timestamp(entry_data)
                        if timestamp and button_label:
                            button_labels[thread_id] = f"{timestamp} · {button_label}"
                        elif timestamp:
                            button_labels[thread_id] = timestamp
                        elif button_label:
                            button_labels[thread_id] = button_label
                        if label == "(no preview)":
                            cached_preview = _thread_summary_preview(record, thread_id)
                            if cached_preview:
                                label = cached_preview
                    items.append((thread_id, label))
            else:
                for entry in entries_by_id.values():
                    entry_id = entry.get("id")
                    if not isinstance(entry_id, str) or not entry_id:
                        continue
                    label = _format_thread_preview(entry)
                    button_label = _extract_first_user_preview(entry)
                    timestamp = _format_resume_timestamp(entry)
                    if timestamp and button_label:
                        button_labels[entry_id] = f"{timestamp} · {button_label}"
                    elif timestamp:
                        button_labels[entry_id] = timestamp
                    elif button_label:
                        button_labels[entry_id] = button_label
                    items.append((entry_id, label))
        if missing_ids:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.resume.missing_thread_metadata",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                stored_count=len(record.thread_ids),
                listed_count=(
                    len(entries_by_id)
                    if not args.show_unscoped
                    else len(thread_data.threads)
                ),
                missing_ids=missing_ids[:RESUME_MISSING_IDS_LOG_LIMIT],
                list_failed=thread_data.list_failed,
            )
        if not items:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No resumable threads found.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        state = SelectionState(
            items=items,
            button_labels=button_labels,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _refresh_thread_summaries(
        self,
        client: CodexAppServerClient,
        thread_ids: Sequence[str],
        *,
        topic_keys_by_thread: Optional[dict[str, set[str]]] = None,
        default_topic_key: Optional[str] = None,
    ) -> set[str]:
        refreshed: set[str] = set()
        if not thread_ids:
            return refreshed
        unique_ids: list[str] = []
        seen: set[str] = set()
        for thread_id in thread_ids:
            if not isinstance(thread_id, str) or not thread_id:
                continue
            if thread_id in seen:
                continue
            seen.add(thread_id)
            unique_ids.append(thread_id)
            if len(unique_ids) >= RESUME_REFRESH_LIMIT:
                break
        for thread_id in unique_ids:
            try:
                result = await client.thread_resume(thread_id)
            except (OSError, RuntimeError, ValueError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.refresh_failed",
                    thread_id=thread_id,
                    exc=exc,
                )
                continue
            user_preview, assistant_preview = _extract_thread_preview_parts(result)
            info = _extract_thread_info(result)
            workspace_path = info.get("workspace_path")
            rollout_path = info.get("rollout_path")
            if (
                user_preview is None
                and assistant_preview is None
                and workspace_path is None
                and rollout_path is None
            ):
                continue
            last_used_at = now_iso() if user_preview or assistant_preview else None

            def apply(
                record: "TelegramTopicRecord",
                *,
                thread_id: str = thread_id,
                user_preview: Optional[str] = user_preview,
                assistant_preview: Optional[str] = assistant_preview,
                last_used_at: Optional[str] = last_used_at,
                workspace_path: Optional[str] = workspace_path,
                rollout_path: Optional[str] = rollout_path,
            ) -> None:
                _set_thread_summary(
                    record,
                    thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=workspace_path,
                    rollout_path=rollout_path,
                )

            keys = (
                topic_keys_by_thread.get(thread_id)
                if topic_keys_by_thread is not None
                else None
            )
            if keys:
                for key in keys:
                    await self._store.update_topic(key, apply)
            elif default_topic_key:
                await self._store.update_topic(default_topic_key, apply)
            else:
                continue
            refreshed.add(thread_id)
        return refreshed

    async def _list_threads_paginated(
        self,
        client: CodexAppServerClient,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entries: list[dict[str, Any]] = []
        found_ids: set[str] = set()
        seen_ids: set[str] = set()
        cursor: Optional[str] = None
        page_count = max(1, max_pages)
        for _ in range(page_count):
            payload = await client.thread_list(cursor=cursor, limit=limit)
            page_entries = _coerce_thread_list(payload)
            for entry in page_entries:
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if isinstance(thread_id, str):
                    if thread_id in seen_ids:
                        continue
                    seen_ids.add(thread_id)
                    found_ids.add(thread_id)
                entries.append(entry)
            if needed_ids is not None and needed_ids.issubset(found_ids):
                break
            cursor = _extract_thread_list_cursor(payload)
            if not cursor:
                break
        return entries, found_ids

    async def _selection_resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        callback_answered = False

        async def _answer_once(text: str) -> None:
            nonlocal callback_answered
            if callback_answered or callback is None:
                return
            await self._answer_callback(callback, text)
            callback_answered = True

        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is not None and self._effective_agent(record) == "opencode":
            await self._resume_opencode_thread_by_id(key, thread_id, callback=callback)
            return
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    error or TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        record = self._record_with_workspace_path(record, workspace_path)
        try:
            await _answer_once("Resuming...")
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=chat_id,
                thread_id=thread_id_val,
                exc=exc,
            )
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    APP_SERVER_UNAVAILABLE_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if client is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            result = await client.thread_resume(thread_id)
        except (OSError, RuntimeError, ValueError, CodexAppServerError) as exc:
            if is_missing_thread_error(exc):
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.resume.missing_thread",
                    topic_key=key,
                    thread_id=thread_id,
                )

                def clear_stale(record: "TelegramTopicRecord") -> None:
                    if record.active_thread_id == thread_id:
                        record.active_thread_id = None
                    if thread_id in record.thread_ids:
                        record.thread_ids.remove(thread_id)
                    record.thread_summaries.pop(thread_id, None)

                await self._store.update_topic(key, clear_stale)
                await _answer_once("Thread missing")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread no longer exists. Cleared stale state; use /new to start a fresh thread.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await _answer_once("Resume failed")
            chat_id, thread_id_val = _split_topic_key(key)
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if record is None or not record.workspace_path:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not isinstance(resumed_path, str):
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread metadata missing workspace path; resume aborted to avoid cross-worktree mixups.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            workspace_root = Path(record.workspace_path).expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except OSError:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread workspace path is invalid; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not _paths_compatible(workspace_root, resumed_root):
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread belongs to a different workspace; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return
        sync_binding = True
        if (
            getattr(self, "_hub_root", None) is not None
            or getattr(self._config, "root", None) is not None
        ):
            try:
                from .execution import _resolve_telegram_managed_thread

                (
                    _orchestration_service,
                    managed_thread,
                ) = await _resolve_telegram_managed_thread(
                    self,
                    surface_key=key,
                    workspace_root=workspace_root,
                    agent=self._effective_runtime_agent(record),
                    agent_profile=self._effective_agent_profile(record),
                    repo_id=(
                        record.repo_id.strip()
                        if isinstance(record.repo_id, str) and record.repo_id.strip()
                        else None
                    ),
                    resource_kind=(
                        record.resource_kind.strip()
                        if isinstance(record.resource_kind, str)
                        and record.resource_kind.strip()
                        else None
                    ),
                    resource_id=(
                        record.resource_id.strip()
                        if isinstance(record.resource_id, str)
                        and record.resource_id.strip()
                        else None
                    ),
                    mode="repo",
                    pma_enabled=False,
                    backend_thread_id=thread_id,
                    allow_new_thread=True,
                )
                if managed_thread is None:
                    raise RuntimeError("managed thread resolution returned no thread")
                sync_binding = False
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.binding_failed",
                    topic_key=key,
                    thread_id=thread_id,
                    exc=exc,
                )
                await _answer_once("Resume failed")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Failed to rebind the managed thread; check logs for details.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
        updated_record = await self._apply_thread_result(
            chat_id,
            thread_id_val,
            result,
            active_thread_id=thread_id,
            overwrite_defaults=True,
            sync_binding=sync_binding,
        )
        await _answer_once("Resumed thread")
        message = _format_resume_summary(
            thread_id,
            result,
            workspace_path=updated_record.workspace_path,
            model=updated_record.model,
            effort=updated_record.effort,
            agent=updated_record.agent,
        )
        await self._finalize_selection(key, callback, message)

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        await self._selection_resume_thread_by_id(key, thread_id, callback)

    async def _resume_opencode_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        callback_answered = False

        async def _answer_once(text: str) -> None:
            nonlocal callback_answered
            if callback_answered or callback is None:
                return
            await self._answer_callback(callback, text)
            callback_answered = True

        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    error or TOPIC_NOT_BOUND_RESUME_MESSAGE,
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        record = self._record_with_workspace_path(record, workspace_path)
        supervisor = getattr(self, "_opencode_supervisor", None)
        if supervisor is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Workspace unavailable; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            await _answer_once("Resuming...")
            client = await supervisor.get_client(workspace_root)
            session = await client.get_session(thread_id)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            if self._is_missing_opencode_session_error(exc):
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.resume.missing_thread",
                    topic_key=key,
                    thread_id=thread_id,
                    agent="opencode",
                )

                def clear_stale(record: "TelegramTopicRecord") -> None:
                    if record.active_thread_id == thread_id:
                        record.active_thread_id = None
                    if thread_id in record.thread_ids:
                        record.thread_ids.remove(thread_id)
                    record.thread_summaries.pop(thread_id, None)

                await self._store.update_topic(key, clear_stale)
                await _answer_once("Thread missing")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread no longer exists. Cleared stale state; use /new to start a fresh thread.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.opencode.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await _answer_once("Resume failed")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume OpenCode thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        resumed_path = _extract_opencode_session_path(session)
        if resumed_path:
            try:
                workspace_root = Path(record.workspace_path).expanduser().resolve()
                resumed_root = Path(resumed_path).expanduser().resolve()
            except OSError:
                await _answer_once("Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread workspace path is invalid; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            if not _paths_compatible(workspace_root, resumed_root):
                await _answer_once("Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread belongs to a different workspace; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await _answer_once("Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return

        def apply(record: "TelegramTopicRecord") -> None:
            record.active_thread_id = thread_id
            if thread_id in record.thread_ids:
                record.thread_ids.remove(thread_id)
            record.thread_ids.insert(0, thread_id)
            if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
            _set_thread_summary(
                record,
                thread_id,
                last_used_at=now_iso(),
                workspace_path=record.workspace_path,
                rollout_path=record.rollout_path,
            )

        updated_record = await self._router.update_topic(chat_id, thread_id_val, apply)
        if updated_record is not None and updated_record.workspace_path:
            from .execution import _sync_telegram_thread_binding

            pma_enabled = bool(updated_record.pma_enabled)
            await _sync_telegram_thread_binding(
                self,
                surface_key=key,
                workspace_root=Path(updated_record.workspace_path),
                agent=self._effective_runtime_agent(updated_record),
                repo_id=(
                    updated_record.repo_id.strip()
                    if isinstance(updated_record.repo_id, str)
                    and updated_record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    updated_record.resource_kind.strip()
                    if isinstance(updated_record.resource_kind, str)
                    and updated_record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    updated_record.resource_id.strip()
                    if isinstance(updated_record.resource_id, str)
                    and updated_record.resource_id.strip()
                    else None
                ),
                backend_thread_id=None if pma_enabled else thread_id,
                mode="pma" if pma_enabled else "repo",
                pma_enabled=pma_enabled,
            )
        await self._answer_callback(callback, "Resumed thread")
        summary = None
        if updated_record is not None:
            summary = updated_record.thread_summaries.get(thread_id)
        entry: dict[str, Any] = {}
        if summary is not None:
            entry = {
                "user_preview": summary.user_preview,
                "assistant_preview": summary.assistant_preview,
            }
        message = _format_resume_summary(
            thread_id,
            entry,
            workspace_path=updated_record.workspace_path if updated_record else None,
            model=updated_record.model if updated_record else None,
            effort=updated_record.effort if updated_record else None,
            agent=updated_record.agent if updated_record else None,
        )
        await self._finalize_selection(key, callback, message)
