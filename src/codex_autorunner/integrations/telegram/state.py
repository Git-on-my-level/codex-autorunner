from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast
from urllib.parse import quote, unquote

from ...core.sqlite_utils import connect_sqlite
from ...core.state import now_iso

logger = logging.getLogger("codex_autorunner.integrations.telegram.state")

STATE_VERSION = 5
TOPIC_ROOT = "root"
APPROVAL_MODE_YOLO = "yolo"
APPROVAL_MODE_SAFE = "safe"
APPROVAL_MODES = {APPROVAL_MODE_YOLO, APPROVAL_MODE_SAFE}
AGENT_VALUES = {"codex", "opencode"}
STALE_SCOPED_TOPIC_DAYS = 30
MAX_SCOPED_TOPICS_PER_BASE = 5


def normalize_approval_mode(
    mode: Optional[str], *, default: str = APPROVAL_MODE_YOLO
) -> str:
    if not isinstance(mode, str):
        return default
    key = mode.strip().lower()
    if key in APPROVAL_MODES:
        return key
    return default


def normalize_agent(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    compact = "".join(ch for ch in normalized if ch.isalnum())
    if normalized in AGENT_VALUES:
        return normalized
    if compact in AGENT_VALUES:
        return compact
    return None


def _encode_scope(scope: str) -> str:
    return quote(scope, safe="")


def _decode_scope(scope: str) -> str:
    return unquote(scope)


def topic_key(
    chat_id: int, thread_id: Optional[int], *, scope: Optional[str] = None
) -> str:
    if not isinstance(chat_id, int):
        raise TypeError("chat_id must be int")
    suffix = str(thread_id) if thread_id is not None else TOPIC_ROOT
    base_key = f"{chat_id}:{suffix}"
    if not isinstance(scope, str):
        return base_key
    scope = scope.strip()
    if not scope:
        return base_key
    return f"{base_key}:{_encode_scope(scope)}"


def parse_topic_key(key: str) -> tuple[int, Optional[int], Optional[str]]:
    parts = key.split(":", 2)
    if len(parts) < 2:
        raise ValueError("invalid topic key")
    chat_raw, thread_raw = parts[0], parts[1]
    scope_raw = parts[2] if len(parts) == 3 else None
    if not chat_raw or not thread_raw:
        raise ValueError("invalid topic key")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ValueError("invalid chat id in topic key") from exc
    if thread_raw == TOPIC_ROOT:
        thread_id = None
    else:
        try:
            thread_id = int(thread_raw)
        except ValueError as exc:
            raise ValueError("invalid thread id in topic key") from exc
    scope = None
    if isinstance(scope_raw, str) and scope_raw:
        scope = _decode_scope(scope_raw)
    return chat_id, thread_id, scope


def _parse_iso_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _base_topic_key(raw_key: str) -> Optional[str]:
    try:
        chat_id, thread_id, _scope = parse_topic_key(raw_key)
    except ValueError:
        return None
    return topic_key(chat_id, thread_id)


TELEGRAM_SCHEMA_VERSION = 1


def _parse_json_payload(raw: Optional[str]) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _thread_predicate(thread_id: Optional[int]) -> tuple[str, tuple[Any, ...]]:
    if thread_id is None:
        return "thread_id IS NULL", ()
    return "thread_id = ?", (thread_id,)


@dataclass
class ThreadSummary:
    user_preview: Optional[str] = None
    assistant_preview: Optional[str] = None
    last_used_at: Optional[str] = None
    workspace_path: Optional[str] = None
    rollout_path: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["ThreadSummary"]:
        if not isinstance(payload, dict):
            return None
        user_preview = payload.get("user_preview") or payload.get("userPreview")
        assistant_preview = payload.get("assistant_preview") or payload.get(
            "assistantPreview"
        )
        last_used_at = payload.get("last_used_at") or payload.get("lastUsedAt")
        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        rollout_path = (
            payload.get("rollout_path")
            or payload.get("rolloutPath")
            or payload.get("path")
        )
        if not isinstance(user_preview, str):
            user_preview = None
        if not isinstance(assistant_preview, str):
            assistant_preview = None
        if not isinstance(last_used_at, str):
            last_used_at = None
        if not isinstance(workspace_path, str):
            workspace_path = None
        if not isinstance(rollout_path, str):
            rollout_path = None
        return cls(
            user_preview=user_preview,
            assistant_preview=assistant_preview,
            last_used_at=last_used_at,
            workspace_path=workspace_path,
            rollout_path=rollout_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_preview": self.user_preview,
            "assistant_preview": self.assistant_preview,
            "last_used_at": self.last_used_at,
            "workspace_path": self.workspace_path,
            "rollout_path": self.rollout_path,
        }


@dataclass
class TelegramTopicRecord:
    repo_id: Optional[str] = None
    workspace_path: Optional[str] = None
    workspace_id: Optional[str] = None
    pma_enabled: bool = False
    active_thread_id: Optional[str] = None
    thread_ids: list[str] = dataclasses.field(default_factory=list)
    thread_summaries: dict[str, ThreadSummary] = dataclasses.field(default_factory=dict)
    pending_compact_seed: Optional[str] = None
    pending_compact_seed_thread_id: Optional[str] = None
    last_update_id: Optional[int] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    summary: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[Any] = None
    rollout_path: Optional[str] = None
    approval_mode: str = APPROVAL_MODE_YOLO
    last_active_at: Optional[str] = None
    last_ticket_dispatch_seq: Optional[str] = None

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, default_approval_mode: str
    ) -> "TelegramTopicRecord":
        repo_id = payload.get("repo_id") or payload.get("repoId")
        if not isinstance(repo_id, str):
            repo_id = None
        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        if not isinstance(workspace_path, str):
            workspace_path = None
        workspace_id = payload.get("workspace_id") or payload.get("workspaceId")
        if not isinstance(workspace_id, str):
            workspace_id = None
        pma_enabled = payload.get("pma_enabled") or payload.get("pmaEnabled")
        if not isinstance(pma_enabled, bool):
            pma_enabled = False
        active_thread_id = payload.get("active_thread_id") or payload.get(
            "activeThreadId"
        )
        if not isinstance(active_thread_id, str):
            active_thread_id = None
        thread_ids_raw = payload.get("thread_ids") or payload.get("threadIds")
        thread_ids: list[str] = []
        if isinstance(thread_ids_raw, list):
            for item in thread_ids_raw:
                if isinstance(item, str) and item:
                    thread_ids.append(item)
        thread_summaries_raw = payload.get("thread_summaries") or payload.get(
            "threadSummaries"
        )
        thread_summaries: dict[str, ThreadSummary] = {}
        if isinstance(thread_summaries_raw, dict):
            for thread_id, summary in thread_summaries_raw.items():
                if not isinstance(thread_id, str):
                    continue
                if not isinstance(summary, dict):
                    continue
                parsed = ThreadSummary.from_dict(summary)
                if parsed is None:
                    continue
                thread_summaries[thread_id] = parsed
        pending_compact_seed = payload.get("pending_compact_seed") or payload.get(
            "pendingCompactSeed"
        )
        if not isinstance(pending_compact_seed, str):
            pending_compact_seed = None
        pending_compact_seed_thread_id = payload.get(
            "pending_compact_seed_thread_id"
        ) or payload.get("pendingCompactSeedThreadId")
        if not isinstance(pending_compact_seed_thread_id, str):
            pending_compact_seed_thread_id = None
        if not thread_ids and isinstance(active_thread_id, str):
            thread_ids = [active_thread_id]
        last_update_id = payload.get("last_update_id") or payload.get("lastUpdateId")
        if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
            last_update_id = None
        agent = normalize_agent(payload.get("agent"))
        model = payload.get("model")
        if not isinstance(model, str):
            model = None
        effort = payload.get("effort") or payload.get("reasoningEffort")
        if not isinstance(effort, str):
            effort = None
        summary = payload.get("summary") or payload.get("summaryMode")
        if not isinstance(summary, str):
            summary = None
        approval_policy = payload.get("approval_policy") or payload.get(
            "approvalPolicy"
        )
        if not isinstance(approval_policy, str):
            approval_policy = None
        sandbox_policy = payload.get("sandbox_policy") or payload.get("sandboxPolicy")
        if not isinstance(sandbox_policy, (dict, str)):
            sandbox_policy = None
        rollout_path = (
            payload.get("rollout_path")
            or payload.get("rolloutPath")
            or payload.get("path")
        )
        if not isinstance(rollout_path, str):
            rollout_path = None
        approval_mode = payload.get("approval_mode") or payload.get("approvalMode")
        approval_mode = normalize_approval_mode(
            approval_mode, default=default_approval_mode
        )
        last_active_at = payload.get("last_active_at") or payload.get("lastActiveAt")
        if not isinstance(last_active_at, str):
            last_active_at = None
        last_ticket_dispatch_seq = payload.get(
            "last_ticket_dispatch_seq"
        ) or payload.get("lastTicketDispatchSeq")
        if not isinstance(last_ticket_dispatch_seq, str):
            last_ticket_dispatch_seq = None
        return cls(
            repo_id=repo_id,
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            pma_enabled=pma_enabled,
            active_thread_id=active_thread_id,
            thread_ids=thread_ids,
            thread_summaries=thread_summaries,
            pending_compact_seed=pending_compact_seed,
            pending_compact_seed_thread_id=pending_compact_seed_thread_id,
            last_update_id=last_update_id,
            agent=agent,
            model=model,
            effort=effort,
            summary=summary,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            rollout_path=rollout_path,
            approval_mode=approval_mode,
            last_active_at=last_active_at,
            last_ticket_dispatch_seq=last_ticket_dispatch_seq,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "workspace_path": self.workspace_path,
            "workspace_id": self.workspace_id,
            "pma_enabled": self.pma_enabled,
            "active_thread_id": self.active_thread_id,
            "thread_ids": list(self.thread_ids),
            "thread_summaries": {
                thread_id: summary.to_dict()
                for thread_id, summary in self.thread_summaries.items()
            },
            "pending_compact_seed": self.pending_compact_seed,
            "pending_compact_seed_thread_id": self.pending_compact_seed_thread_id,
            "last_update_id": self.last_update_id,
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "summary": self.summary,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "rollout_path": self.rollout_path,
            "approval_mode": self.approval_mode,
            "last_active_at": self.last_active_at,
            "last_ticket_dispatch_seq": self.last_ticket_dispatch_seq,
        }


@dataclass
class TelegramState:
    version: int = STATE_VERSION
    topics: dict[str, TelegramTopicRecord] = dataclasses.field(default_factory=dict)
    topic_scopes: dict[str, str] = dataclasses.field(default_factory=dict)
    pending_approvals: dict[str, "PendingApprovalRecord"] = dataclasses.field(
        default_factory=dict
    )
    outbox: dict[str, "OutboxRecord"] = dataclasses.field(default_factory=dict)
    pending_voice: dict[str, "PendingVoiceRecord"] = dataclasses.field(
        default_factory=dict
    )
    last_update_id_global: Optional[int] = None

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "topics": {key: record.to_dict() for key, record in self.topics.items()},
            "topic_scopes": dict(self.topic_scopes),
            "pending_approvals": {
                key: record.to_dict() for key, record in self.pending_approvals.items()
            },
            "outbox": {key: record.to_dict() for key, record in self.outbox.items()},
            "pending_voice": {
                key: record.to_dict() for key, record in self.pending_voice.items()
            },
            "last_update_id_global": self.last_update_id_global,
        }
        return json.dumps(payload, indent=2) + "\n"


@dataclass
class PendingApprovalRecord:
    request_id: str
    turn_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: Optional[int]
    prompt: str
    created_at: str
    topic_key: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["PendingApprovalRecord"]:
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("request_id")
        turn_id = payload.get("turn_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        prompt = payload.get("prompt") or ""
        created_at = payload.get("created_at")
        topic_key = payload.get("topic_key") or payload.get("topicKey")
        if not isinstance(request_id, str) or not request_id:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(prompt, str):
            prompt = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(topic_key, str) or not topic_key:
            topic_key = None
        return cls(
            request_id=request_id,
            turn_id=turn_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            prompt=prompt,
            created_at=created_at,
            topic_key=topic_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "topic_key": self.topic_key,
        }


@dataclass
class OutboxRecord:
    record_id: str
    chat_id: int
    thread_id: Optional[int]
    reply_to_message_id: Optional[int]
    placeholder_message_id: Optional[int]
    text: str
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    operation: Optional[str] = None
    message_id: Optional[int] = None
    outbox_key: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["OutboxRecord"]:
        if not isinstance(payload, dict):
            return None
        record_id = payload.get("record_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        reply_to_message_id = payload.get("reply_to_message_id")
        placeholder_message_id = payload.get("placeholder_message_id")
        text = payload.get("text") or ""
        created_at = payload.get("created_at")
        attempts = payload.get("attempts", 0)
        last_error = payload.get("last_error")
        last_attempt_at = payload.get("last_attempt_at")
        next_attempt_at = payload.get("next_attempt_at")
        operation = payload.get("operation")
        message_id = payload.get("message_id")
        outbox_key = payload.get("outbox_key")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if reply_to_message_id is not None and not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        if placeholder_message_id is not None and not isinstance(
            placeholder_message_id, int
        ):
            placeholder_message_id = None
        if not isinstance(text, str):
            text = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        if not isinstance(last_error, str):
            last_error = None
        if not isinstance(last_attempt_at, str):
            last_attempt_at = None
        if not isinstance(next_attempt_at, str):
            next_attempt_at = None
        if not isinstance(operation, str):
            operation = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(outbox_key, str):
            outbox_key = None
        return cls(
            record_id=record_id,
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            placeholder_message_id=placeholder_message_id,
            text=text,
            created_at=created_at,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=last_attempt_at,
            next_attempt_at=next_attempt_at,
            operation=operation,
            message_id=message_id,
            outbox_key=outbox_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "reply_to_message_id": self.reply_to_message_id,
            "placeholder_message_id": self.placeholder_message_id,
            "text": self.text,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "operation": self.operation,
            "message_id": self.message_id,
            "outbox_key": self.outbox_key,
        }


@dataclass
class PendingVoiceRecord:
    record_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: int
    file_id: str
    file_name: Optional[str]
    caption: str
    file_size: Optional[int]
    mime_type: Optional[str]
    duration: Optional[int]
    workspace_path: Optional[str]
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    download_path: Optional[str] = None
    progress_message_id: Optional[int] = None
    transcript_message_id: Optional[int] = None
    transcript_text: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["PendingVoiceRecord"]:
        if not isinstance(payload, dict):
            return None
        record_id = payload.get("record_id")
        chat_id = payload.get("chat_id")
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        file_id = payload.get("file_id")
        file_name = payload.get("file_name")
        caption = payload.get("caption") or ""
        file_size = payload.get("file_size")
        mime_type = payload.get("mime_type")
        duration = payload.get("duration")
        workspace_path = payload.get("workspace_path")
        created_at = payload.get("created_at")
        attempts = payload.get("attempts", 0)
        last_error = payload.get("last_error")
        last_attempt_at = payload.get("last_attempt_at")
        next_attempt_at = payload.get("next_attempt_at")
        download_path = payload.get("download_path")
        progress_message_id = payload.get("progress_message_id")
        transcript_message_id = payload.get("transcript_message_id")
        transcript_text = payload.get("transcript_text")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if not isinstance(message_id, int):
            return None
        if not isinstance(file_id, str) or not file_id:
            return None
        if not isinstance(file_name, str):
            file_name = None
        if not isinstance(caption, str):
            caption = ""
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(mime_type, str):
            mime_type = None
        if duration is not None and not isinstance(duration, int):
            duration = None
        if not isinstance(workspace_path, str):
            workspace_path = None
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        if not isinstance(last_error, str):
            last_error = None
        if not isinstance(last_attempt_at, str):
            last_attempt_at = None
        if not isinstance(next_attempt_at, str):
            next_attempt_at = None
        if not isinstance(download_path, str):
            download_path = None
        if progress_message_id is not None and not isinstance(progress_message_id, int):
            progress_message_id = None
        if transcript_message_id is not None and not isinstance(
            transcript_message_id, int
        ):
            transcript_message_id = None
        if not isinstance(transcript_text, str):
            transcript_text = None
        return cls(
            record_id=record_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            file_id=file_id,
            file_name=file_name,
            caption=caption,
            file_size=file_size,
            mime_type=mime_type,
            duration=duration,
            workspace_path=workspace_path,
            created_at=created_at,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=last_attempt_at,
            next_attempt_at=next_attempt_at,
            download_path=download_path,
            progress_message_id=progress_message_id,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "caption": self.caption,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "duration": self.duration,
            "workspace_path": self.workspace_path,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "download_path": self.download_path,
            "progress_message_id": self.progress_message_id,
            "transcript_message_id": self.transcript_message_id,
            "transcript_text": self.transcript_text,
        }


class TelegramStateStore:
    def __init__(
        self, path: Path, *, default_approval_mode: str = APPROVAL_MODE_YOLO
    ) -> None:
        self._path = path
        self._default_approval_mode = normalize_approval_mode(default_approval_mode)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="telegram-state"
        )
        self._connection: Optional[sqlite3.Connection] = None

    @property
    def path(self) -> Path:
        return self._path

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    async def load(self) -> TelegramState:
        return await self._run(self._load_state_sync)

    async def save(self, state: TelegramState) -> None:
        await self._run(self._save_state_sync, state)

    async def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return await self._run(self._get_topic_sync, key)

    async def list_topics(self) -> dict[str, TelegramTopicRecord]:
        """Return all stored topics keyed by topic_key."""
        return await self._run(self._list_topics_sync)

    def _list_topics_sync(self) -> dict[str, TelegramTopicRecord]:
        conn = self._ensure_connection()
        cursor = conn.execute("SELECT topic_key, payload_json FROM telegram_topics")
        topics: dict[str, TelegramTopicRecord] = {}
        for key, payload_json in cursor.fetchall():
            try:
                payload = (
                    json.loads(payload_json) if isinstance(payload_json, str) else {}
                )
            except Exception:
                payload = {}
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[str(key)] = record
        return topics

    async def get_topic_scope(self, key: str) -> Optional[str]:
        return await self._run(self._get_topic_scope_sync, key)

    async def set_topic_scope(self, key: str, scope: Optional[str]) -> None:
        await self._run(self._set_topic_scope_sync, key, scope)

    async def bind_topic(
        self, key: str, workspace_path: str, *, repo_id: Optional[str] = None
    ) -> TelegramTopicRecord:
        if not isinstance(workspace_path, str) or not workspace_path:
            raise ValueError("workspace_path is required")

        def apply(record: TelegramTopicRecord) -> None:
            # Switching workspaces should restart the app-server thread in the new repo.
            record.workspace_path = workspace_path
            record.workspace_id = None
            if repo_id is not None:
                record.repo_id = repo_id
            record.active_thread_id = None
            record.thread_ids = []
            record.thread_summaries = {}
            record.rollout_path = None
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None

        return await self._update_topic(key, apply)

    async def set_active_thread(
        self, key: str, thread_id: Optional[str]
    ) -> TelegramTopicRecord:
        def apply(record: TelegramTopicRecord) -> None:
            record.active_thread_id = thread_id

        return await self._update_topic(key, apply)

    async def find_active_thread(
        self, thread_id: str, *, exclude_key: Optional[str] = None
    ) -> Optional[str]:
        if not isinstance(thread_id, str) or not thread_id:
            return None
        return await self._run(self._find_active_thread_sync, thread_id, exclude_key)

    async def set_approval_mode(self, key: str, mode: str) -> TelegramTopicRecord:
        normalized = normalize_approval_mode(mode, default=self._default_approval_mode)

        def apply(record: TelegramTopicRecord) -> None:
            record.approval_mode = normalized

        return await self._update_topic(key, apply)

    async def ensure_topic(self, key: str) -> TelegramTopicRecord:
        def apply(_record: TelegramTopicRecord) -> None:
            pass

        return await self._update_topic(key, apply)

    async def update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        return await self._update_topic(key, apply)

    async def upsert_pending_approval(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        return await self._run(self._upsert_pending_approval_sync, record)

    async def clear_pending_approval(self, request_id: str) -> None:
        await self._run(self._clear_pending_approval_sync, request_id)

    async def pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> list[PendingApprovalRecord]:
        return await self._run(
            self._pending_approvals_for_topic_sync, chat_id, thread_id
        )

    async def clear_pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        await self._run(
            self._clear_pending_approvals_for_topic_sync, chat_id, thread_id
        )

    async def pending_approvals_for_key(self, key: str) -> list[PendingApprovalRecord]:
        return await self._run(self._pending_approvals_for_key_sync, key)

    async def clear_pending_approvals_for_key(self, key: str) -> None:
        await self._run(self._clear_pending_approvals_for_key_sync, key)

    async def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def update_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def delete_outbox(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

    async def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        return await self._run(self._get_outbox_sync, record_id)

    async def list_outbox(self) -> list[OutboxRecord]:
        return await self._run(self._list_outbox_sync)

    async def enqueue_pending_voice(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        return await self._run(self._upsert_pending_voice_sync, record)

    async def update_pending_voice(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        return await self._run(self._upsert_pending_voice_sync, record)

    async def delete_pending_voice(self, record_id: str) -> None:
        await self._run(self._delete_pending_voice_sync, record_id)

    async def get_pending_voice(self, record_id: str) -> Optional[PendingVoiceRecord]:
        return await self._run(self._get_pending_voice_sync, record_id)

    async def list_pending_voice(self) -> list[PendingVoiceRecord]:
        return await self._run(self._list_pending_voice_sync)

    async def get_last_update_id_global(self) -> Optional[int]:
        return await self._run(self._get_last_update_id_global_sync)

    async def update_last_update_id_global(self, update_id: int) -> Optional[int]:
        return await self._run(self._update_last_update_id_global_sync, update_id)

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    def _ensure_connection(self) -> sqlite3.Connection:
        # Backwards-compatible helper used by older call sites.
        # _connection_sync() remains the single source of truth for opening the DB.
        return self._connection_sync()

    def _connection_sync(self) -> sqlite3.Connection:
        if self._connection is None:
            conn = connect_sqlite(self._path)
            self._ensure_schema(conn)
            self._connection = conn
            self._maybe_migrate_legacy(conn)
        return self._connection

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topic_scopes (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            self._dedupe_topic_scopes(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_scopes_root
                    ON telegram_topic_scopes(chat_id)
                    WHERE thread_id IS NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_scopes_thread
                    ON telegram_topic_scopes(chat_id, thread_id)
                    WHERE thread_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_topics (
                    topic_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    scope TEXT,
                    workspace_path TEXT,
                    repo_id TEXT,
                    active_thread_id TEXT,
                    last_update_id INTEGER,
                    approval_mode TEXT,
                    last_active_at TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_chat_thread
                    ON telegram_topics(chat_id, thread_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_workspace
                    ON telegram_topics(workspace_path)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_topics_last_active
                    ON telegram_topics(last_active_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_pending_approvals (
                    request_id TEXT PRIMARY KEY,
                    topic_key TEXT,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_approvals_topic
                    ON telegram_pending_approvals(topic_key)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_approvals_expires
                    ON telegram_pending_approvals(expires_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    record_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    operation TEXT,
                    message_id INTEGER,
                    outbox_key TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            # Ensure legacy DBs gain the newer columns before creating indexes that
            # reference them. The ALTERs are idempotent and cheap.
            for col, col_type in [
                ("next_attempt_at", "TEXT"),
                ("operation", "TEXT"),
                ("message_id", "INTEGER"),
                ("outbox_key", "TEXT"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE telegram_outbox ADD COLUMN {col} {col_type}"
                    )
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_outbox_created
                    ON telegram_outbox(created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_outbox_key
                    ON telegram_outbox(outbox_key)
                    WHERE outbox_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_pending_voice (
                    record_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_attempt_at TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tg_voice_next_attempt
                    ON telegram_pending_voice(next_attempt_at)
                """
            )
            now = now_iso()
            self._set_meta(conn, "schema_version", str(TELEGRAM_SCHEMA_VERSION), now)
            self._set_meta(conn, "state_version", str(STATE_VERSION), now)

    def _dedupe_topic_scopes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            DELETE FROM telegram_topic_scopes
             WHERE thread_id IS NULL
               AND EXISTS (
                SELECT 1
                  FROM telegram_topic_scopes AS newer
                 WHERE newer.thread_id IS NULL
                   AND newer.chat_id = telegram_topic_scopes.chat_id
                   AND (
                    newer.updated_at > telegram_topic_scopes.updated_at
                    OR (
                        newer.updated_at = telegram_topic_scopes.updated_at
                        AND newer.rowid > telegram_topic_scopes.rowid
                    )
                   )
               )
            """
        )
        conn.execute(
            """
            DELETE FROM telegram_topic_scopes
             WHERE thread_id IS NOT NULL
               AND EXISTS (
                SELECT 1
                  FROM telegram_topic_scopes AS newer
                 WHERE newer.thread_id IS NOT NULL
                   AND newer.chat_id = telegram_topic_scopes.chat_id
                   AND newer.thread_id = telegram_topic_scopes.thread_id
                   AND (
                    newer.updated_at > telegram_topic_scopes.updated_at
                    OR (
                        newer.updated_at = telegram_topic_scopes.updated_at
                        AND newer.rowid > telegram_topic_scopes.rowid
                    )
                   )
               )
            """
        )

    def _has_persisted_rows(self, conn: sqlite3.Connection) -> bool:
        for table in (
            "telegram_topics",
            "telegram_topic_scopes",
            "telegram_pending_approvals",
            "telegram_outbox",
            "telegram_pending_voice",
        ):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            if row is not None:
                return True
        if self._get_meta(conn, "last_update_id_global") is not None:
            return True
        return False

    def _load_legacy_state_json(self, path: Path) -> Optional[TelegramState]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # The path may already be a SQLite file (e.g., state_file still ends
            # with .json after the migration to SQLite). In that case, ignore the
            # legacy load attempt and treat the DB as the source of truth.
            return None
        if not isinstance(payload, dict):
            return None
        raw_version = payload.get("version")
        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            version = raw_version
        else:
            version = STATE_VERSION
        topics: dict[str, TelegramTopicRecord] = {}
        topics_payload = payload.get("topics")
        if isinstance(topics_payload, dict):
            for key, record_payload in topics_payload.items():
                if not isinstance(key, str) or not key:
                    continue
                record = TelegramTopicRecord.from_dict(
                    record_payload, default_approval_mode=self._default_approval_mode
                )
                if record is not None:
                    topics[key] = record
        topic_scopes: dict[str, str] = {}
        scopes_payload = payload.get("topic_scopes")
        if isinstance(scopes_payload, dict):
            for key, scope in scopes_payload.items():
                if not isinstance(key, str) or not key:
                    continue
                if isinstance(scope, str) and scope:
                    topic_scopes[key] = scope
        pending_approvals: dict[str, PendingApprovalRecord] = {}
        approvals_payload = payload.get("pending_approvals")
        if isinstance(approvals_payload, dict):
            for request_id, record_payload in approvals_payload.items():
                record = PendingApprovalRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.request_id or request_id
                if key:
                    pending_approvals[key] = record
        outbox: dict[str, OutboxRecord] = {}
        outbox_payload = payload.get("outbox")
        if isinstance(outbox_payload, dict):
            for record_id, record_payload in outbox_payload.items():
                record = OutboxRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.record_id or record_id
                if key:
                    outbox[key] = record
        pending_voice: dict[str, PendingVoiceRecord] = {}
        voice_payload = payload.get("pending_voice")
        if isinstance(voice_payload, dict):
            for record_id, record_payload in voice_payload.items():
                record = PendingVoiceRecord.from_dict(record_payload)
                if record is None:
                    continue
                key = record.record_id or record_id
                if key:
                    pending_voice[key] = record
        last_update_id_global = None
        raw_update_id = payload.get("last_update_id_global")
        if isinstance(raw_update_id, int) and not isinstance(raw_update_id, bool):
            last_update_id_global = raw_update_id
        return TelegramState(
            version=version,
            topics=topics,
            topic_scopes=topic_scopes,
            pending_approvals=pending_approvals,
            outbox=outbox,
            pending_voice=pending_voice,
            last_update_id_global=last_update_id_global,
        )

    def _maybe_migrate_legacy(self, conn: sqlite3.Connection) -> None:
        legacy_path = self._path.with_name("telegram_state.json")
        # Legacy JSON migration (remove after old telegram_state.json is retired).
        if not legacy_path.exists():
            return
        if self._has_persisted_rows(conn):
            return
        state = self._load_legacy_state_json(legacy_path)
        if state is None:
            return
        self._save_state_sync(state)

    def _set_meta(
        self, conn: sqlite3.Connection, key: str, value: str, updated_at: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO telegram_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, updated_at),
        )

    def _get_meta(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM telegram_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        value = row["value"]
        return value if isinstance(value, str) else None

    def _get_topic_scope_by_ids(
        self, conn: sqlite3.Connection, chat_id: int, thread_id: Optional[int]
    ) -> Optional[str]:
        clause, params = _thread_predicate(thread_id)
        row = conn.execute(
            f"SELECT scope FROM telegram_topic_scopes WHERE chat_id = ? AND {clause}",
            (chat_id, *params),
        ).fetchone()
        scope = row["scope"] if row else None
        if isinstance(scope, str) and scope:
            return scope
        return None

    def _upsert_topic(
        self,
        conn: sqlite3.Connection,
        key: str,
        record: TelegramTopicRecord,
        created_at: str,
        updated_at: str,
    ) -> None:
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except ValueError:
            return
        approval_mode = normalize_approval_mode(
            record.approval_mode, default=self._default_approval_mode
        )
        last_update_id = record.last_update_id
        if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
            last_update_id = None
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        conn.execute(
            """
            INSERT INTO telegram_topics (
                topic_key,
                chat_id,
                thread_id,
                scope,
                workspace_path,
                repo_id,
                active_thread_id,
                last_update_id,
                approval_mode,
                last_active_at,
                payload_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_key) DO UPDATE SET
                chat_id=excluded.chat_id,
                thread_id=excluded.thread_id,
                scope=excluded.scope,
                workspace_path=excluded.workspace_path,
                repo_id=excluded.repo_id,
                active_thread_id=excluded.active_thread_id,
                last_update_id=excluded.last_update_id,
                approval_mode=excluded.approval_mode,
                last_active_at=excluded.last_active_at,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                key,
                chat_id,
                thread_id,
                scope,
                record.workspace_path,
                record.repo_id,
                record.active_thread_id,
                last_update_id,
                approval_mode,
                record.last_active_at,
                payload_json,
                created_at,
                updated_at,
            ),
        )

    def _load_state_sync(self) -> TelegramState:
        conn = self._connection_sync()
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM telegram_meta")
        }
        version = STATE_VERSION
        raw_version = meta.get("state_version")
        if isinstance(raw_version, str):
            try:
                version = int(raw_version)
            except ValueError:
                version = STATE_VERSION
        last_update_id_global: Optional[int] = None
        raw_update_id = meta.get("last_update_id_global")
        if isinstance(raw_update_id, str):
            try:
                parsed = int(raw_update_id)
            except ValueError:
                parsed = None
            if parsed is not None:
                last_update_id_global = parsed
        topics: dict[str, TelegramTopicRecord] = {}
        for row in conn.execute("SELECT topic_key, payload_json FROM telegram_topics"):
            payload = _parse_json_payload(row["payload_json"])
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[row["topic_key"]] = record
        topic_scopes: dict[str, str] = {}
        for row in conn.execute(
            "SELECT chat_id, thread_id, scope FROM telegram_topic_scopes"
        ):
            scope = row["scope"]
            if isinstance(scope, str) and scope:
                topic_scopes[topic_key(row["chat_id"], row["thread_id"])] = scope
        pending_approvals: dict[str, PendingApprovalRecord] = {}
        for row in conn.execute(
            "SELECT request_id, payload_json FROM telegram_pending_approvals"
        ):
            payload = _parse_json_payload(row["payload_json"])
            approval_record = PendingApprovalRecord.from_dict(payload)
            if approval_record is None:
                continue
            pending_approvals[row["request_id"]] = approval_record
        outbox: dict[str, OutboxRecord] = {}
        for row in conn.execute("SELECT record_id, payload_json FROM telegram_outbox"):
            payload = _parse_json_payload(row["payload_json"])
            outbox_record = OutboxRecord.from_dict(payload)
            if outbox_record is None:
                continue
            outbox[row["record_id"]] = outbox_record
        pending_voice: dict[str, PendingVoiceRecord] = {}
        for row in conn.execute(
            "SELECT record_id, payload_json FROM telegram_pending_voice"
        ):
            payload = _parse_json_payload(row["payload_json"])
            voice_record = PendingVoiceRecord.from_dict(payload)
            if voice_record is None:
                continue
            pending_voice[row["record_id"]] = voice_record
        return TelegramState(
            version=version,
            topics=topics,
            topic_scopes=topic_scopes,
            pending_approvals=pending_approvals,
            outbox=outbox,
            pending_voice=pending_voice,
            last_update_id_global=last_update_id_global,
        )

    def _save_state_sync(self, state: TelegramState) -> None:
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute("DELETE FROM telegram_topic_scopes")
            for key, scope in state.topic_scopes.items():
                try:
                    chat_id, thread_id, _scope = parse_topic_key(key)
                except ValueError:
                    continue
                if not isinstance(scope, str) or not scope:
                    continue
                conn.execute(
                    """
                    INSERT INTO telegram_topic_scopes (
                        chat_id,
                        thread_id,
                        scope,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (chat_id, thread_id, scope, now),
                )
            conn.execute("DELETE FROM telegram_topics")
            for key, record in state.topics.items():
                self._upsert_topic(conn, key, record, now, now)
            conn.execute("DELETE FROM telegram_pending_approvals")
            for record in state.pending_approvals.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_pending_approvals (
                        request_id,
                        topic_key,
                        chat_id,
                        thread_id,
                        created_at,
                        expires_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.request_id,
                        record.topic_key,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        None,
                        payload_json,
                    ),
                )
            conn.execute("DELETE FROM telegram_outbox")
            for record in state.outbox.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_outbox (
                        record_id,
                        chat_id,
                        thread_id,
                        created_at,
                        updated_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        now,
                        payload_json,
                    ),
                )
            conn.execute("DELETE FROM telegram_pending_voice")
            for record in state.pending_voice.values():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO telegram_pending_voice (
                        record_id,
                        chat_id,
                        thread_id,
                        created_at,
                        updated_at,
                        next_attempt_at,
                        payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.chat_id,
                        record.thread_id,
                        record.created_at,
                        now,
                        record.next_attempt_at,
                        payload_json,
                    ),
                )
            self._set_meta(conn, "state_version", str(state.version), now)
            if state.last_update_id_global is not None:
                self._set_meta(
                    conn,
                    "last_update_id_global",
                    str(state.last_update_id_global),
                    now,
                )

    def _get_topic_sync(self, key: str) -> Optional[TelegramTopicRecord]:
        if not isinstance(key, str) or not key:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return TelegramTopicRecord.from_dict(
            payload, default_approval_mode=self._default_approval_mode
        )

    def _get_topic_scope_sync(self, key: str) -> Optional[str]:
        if not isinstance(key, str) or not key:
            return None
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
        except ValueError:
            return None
        return self._get_topic_scope_by_ids(self._connection_sync(), chat_id, thread_id)

    def _set_topic_scope_sync(self, key: str, scope: Optional[str]) -> None:
        if not isinstance(key, str) or not key:
            return
        try:
            chat_id, thread_id, _scope = parse_topic_key(key)
        except ValueError:
            return
        conn = self._connection_sync()
        now = now_iso()
        clause, params = _thread_predicate(thread_id)
        with conn:
            if isinstance(scope, str) and scope:
                if thread_id is None:
                    conn.execute(
                        """
                        INSERT INTO telegram_topic_scopes (
                            chat_id,
                            thread_id,
                            scope,
                            updated_at
                        )
                        VALUES (?, NULL, ?, ?)
                        ON CONFLICT(chat_id) WHERE thread_id IS NULL
                        DO UPDATE SET
                            scope=excluded.scope,
                            updated_at=excluded.updated_at
                        """,
                        (chat_id, scope, now),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO telegram_topic_scopes (
                            chat_id,
                            thread_id,
                            scope,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(chat_id, thread_id) WHERE thread_id IS NOT NULL
                        DO UPDATE SET
                            scope=excluded.scope,
                            updated_at=excluded.updated_at
                        """,
                        (chat_id, thread_id, scope, now),
                    )
            else:
                conn.execute(
                    f"DELETE FROM telegram_topic_scopes WHERE chat_id = ? AND {clause}",
                    (chat_id, *params),
                )
            self._compact_scoped_topics(conn, key)

    async def _update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        return await self._run(self._update_topic_sync, key, apply)

    def _update_topic_sync(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json, created_at FROM telegram_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            record = TelegramTopicRecord(approval_mode=self._default_approval_mode)
            created_at = now_iso()
        else:
            payload = _parse_json_payload(row["payload_json"])
            record = TelegramTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            created_at = row["created_at"] if row["created_at"] else now_iso()
        apply(record)
        record.approval_mode = normalize_approval_mode(
            record.approval_mode, default=self._default_approval_mode
        )
        now = now_iso()
        record.last_active_at = now
        with conn:
            self._upsert_topic(conn, key, record, created_at, now)
        return record

    def _compact_scoped_topics(self, conn: sqlite3.Connection, base_key: str) -> None:
        base_key_normalized = _base_topic_key(base_key)
        if not base_key_normalized:
            return
        try:
            chat_id, thread_id, _scope = parse_topic_key(base_key_normalized)
        except ValueError:
            return
        scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        current_key = (
            topic_key(chat_id, thread_id, scope=scope)
            if isinstance(scope, str) and scope
            else base_key_normalized
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_SCOPED_TOPIC_DAYS)
        clause, params = _thread_predicate(thread_id)
        candidates: list[tuple[str, Optional[str], Optional[datetime]]] = []
        for row in conn.execute(
            f"""
            SELECT topic_key, active_thread_id, last_active_at
              FROM telegram_topics
             WHERE chat_id = ? AND {clause}
            """,
            (chat_id, *params),
        ):
            last_active = _parse_iso_timestamp(row["last_active_at"])
            candidates.append((row["topic_key"], row["active_thread_id"], last_active))
        if not candidates:
            return
        keys_to_remove: set[str] = set()
        for key, active_thread_id, last_active in candidates:
            if key == current_key or active_thread_id:
                continue
            if last_active is None or last_active < cutoff:
                keys_to_remove.add(key)
        remaining = [
            (key, active_thread_id, last_active)
            for key, active_thread_id, last_active in candidates
            if key not in keys_to_remove and key != current_key
        ]
        remaining.sort(
            key=lambda item: item[2] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        current_exists = {key for key, _active, _last in candidates}
        keep_limit = MAX_SCOPED_TOPICS_PER_BASE - (
            1 if current_key in current_exists else 0
        )
        keep_limit = max(0, keep_limit)
        for key, active_thread_id, _last_active in remaining[keep_limit:]:
            if active_thread_id:
                continue
            keys_to_remove.add(key)
        if keys_to_remove:
            conn.executemany(
                "DELETE FROM telegram_topics WHERE topic_key = ?",
                [(key,) for key in keys_to_remove],
            )

    def _find_active_thread_sync(
        self, thread_id: str, exclude_key: Optional[str]
    ) -> Optional[str]:
        conn = self._connection_sync()
        for row in conn.execute(
            """
            SELECT topic_key, chat_id, thread_id
              FROM telegram_topics
             WHERE active_thread_id = ?
            """,
            (thread_id,),
        ):
            key = row["topic_key"]
            if exclude_key and key == exclude_key:
                continue
            base_key = topic_key(row["chat_id"], row["thread_id"])
            scope = self._get_topic_scope_by_ids(conn, row["chat_id"], row["thread_id"])
            resolved_key = (
                topic_key(row["chat_id"], row["thread_id"], scope=scope)
                if isinstance(scope, str) and scope
                else base_key
            )
            if key == resolved_key:
                return key
        return None

    def _upsert_pending_approval_sync(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_pending_approvals (
                    request_id,
                    topic_key,
                    chat_id,
                    thread_id,
                    created_at,
                    expires_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    topic_key=excluded.topic_key,
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    payload_json=excluded.payload_json
                """,
                (
                    record.request_id,
                    record.topic_key,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    None,
                    payload_json,
                ),
            )
        return record

    def _clear_pending_approval_sync(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_pending_approvals WHERE request_id = ?",
                (request_id,),
            )

    def _pending_approvals_for_topic_sync(
        self, chat_id: int, thread_id: Optional[int]
    ) -> list[PendingApprovalRecord]:
        conn = self._connection_sync()
        clause, params = _thread_predicate(thread_id)
        pending: list[PendingApprovalRecord] = []
        for row in conn.execute(
            f"""
            SELECT payload_json
              FROM telegram_pending_approvals
             WHERE chat_id = ? AND {clause}
            """,
            (chat_id, *params),
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                pending.append(record)
        return pending

    def _clear_pending_approvals_for_topic_sync(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        conn = self._connection_sync()
        clause, params = _thread_predicate(thread_id)
        with conn:
            conn.execute(
                f"""
                DELETE FROM telegram_pending_approvals
                 WHERE chat_id = ? AND {clause}
                """,
                (chat_id, *params),
            )

    def _pending_approvals_for_key_sync(self, key: str) -> list[PendingApprovalRecord]:
        if not isinstance(key, str) or not key:
            return []
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except (ValueError, KeyError) as exc:
            logger.debug("Failed to parse topic key '%s': %s", key, exc)
            return []
        conn = self._connection_sync()
        allow_legacy = False
        base_scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        if scope is None and base_scope is None:
            allow_legacy = True
        pending: list[PendingApprovalRecord] = []
        if allow_legacy:
            clause, params = _thread_predicate(thread_id)
            rows = conn.execute(
                f"""
                SELECT payload_json
                  FROM telegram_pending_approvals
                 WHERE topic_key = ?
                    OR (topic_key IS NULL AND chat_id = ? AND {clause})
                """,
                (key, chat_id, *params),
            )
        else:
            rows = conn.execute(
                """
                SELECT payload_json
                  FROM telegram_pending_approvals
                 WHERE topic_key = ?
                """,
                (key,),
            )
        for row in rows:
            payload = _parse_json_payload(row["payload_json"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                pending.append(record)
        return pending

    def _clear_pending_approvals_for_key_sync(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
        except (ValueError, KeyError) as exc:
            logger.debug("Failed to parse topic key '%s': %s", key, exc)
            return
        conn = self._connection_sync()
        base_scope = self._get_topic_scope_by_ids(conn, chat_id, thread_id)
        allow_legacy = scope is None and base_scope is None
        with conn:
            if allow_legacy:
                clause, params = _thread_predicate(thread_id)
                conn.execute(
                    f"""
                    DELETE FROM telegram_pending_approvals
                     WHERE topic_key = ?
                        OR (topic_key IS NULL AND chat_id = ? AND {clause})
                    """,
                    (key, chat_id, *params),
                )
            else:
                conn.execute(
                    "DELETE FROM telegram_pending_approvals WHERE topic_key = ?",
                    (key,),
                )

    def _upsert_outbox_sync(self, record: OutboxRecord) -> OutboxRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        updated_at = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_outbox (
                    record_id,
                    chat_id,
                    thread_id,
                    created_at,
                    updated_at,
                    next_attempt_at,
                    operation,
                    message_id,
                    outbox_key,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    next_attempt_at=excluded.next_attempt_at,
                    operation=excluded.operation,
                    message_id=excluded.message_id,
                    outbox_key=excluded.outbox_key,
                    payload_json=excluded.payload_json
                """,
                (
                    record.record_id,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    updated_at,
                    record.next_attempt_at,
                    record.operation,
                    record.message_id,
                    record.outbox_key,
                    payload_json,
                ),
            )
        return record

    def _delete_outbox_sync(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_outbox WHERE record_id = ?",
                (record_id,),
            )

    def _get_outbox_sync(self, record_id: str) -> Optional[OutboxRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_outbox WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return OutboxRecord.from_dict(payload)

    def _list_outbox_sync(self) -> list[OutboxRecord]:
        conn = self._connection_sync()
        records: list[OutboxRecord] = []
        for row in conn.execute(
            "SELECT payload_json FROM telegram_outbox ORDER BY created_at"
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = OutboxRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _upsert_pending_voice_sync(
        self, record: PendingVoiceRecord
    ) -> PendingVoiceRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        updated_at = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO telegram_pending_voice (
                    record_id,
                    chat_id,
                    thread_id,
                    created_at,
                    updated_at,
                    next_attempt_at,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    next_attempt_at=excluded.next_attempt_at,
                    payload_json=excluded.payload_json
                """,
                (
                    record.record_id,
                    record.chat_id,
                    record.thread_id,
                    record.created_at,
                    updated_at,
                    record.next_attempt_at,
                    payload_json,
                ),
            )
        return record

    def _delete_pending_voice_sync(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM telegram_pending_voice WHERE record_id = ?",
                (record_id,),
            )

    def _get_pending_voice_sync(self, record_id: str) -> Optional[PendingVoiceRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT payload_json FROM telegram_pending_voice WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["payload_json"])
        return PendingVoiceRecord.from_dict(payload)

    def _list_pending_voice_sync(self) -> list[PendingVoiceRecord]:
        conn = self._connection_sync()
        records: list[PendingVoiceRecord] = []
        for row in conn.execute(
            "SELECT payload_json FROM telegram_pending_voice ORDER BY created_at"
        ):
            payload = _parse_json_payload(row["payload_json"])
            record = PendingVoiceRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _get_last_update_id_global_sync(self) -> Optional[int]:
        conn = self._connection_sync()
        value = self._get_meta(conn, "last_update_id_global")
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed

    def _update_last_update_id_global_sync(self, update_id: int) -> Optional[int]:
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return None
        conn = self._connection_sync()
        current = self._get_last_update_id_global_sync()
        if current is None or update_id > current:
            with conn:
                self._set_meta(conn, "last_update_id_global", str(update_id), now_iso())
            return update_id
        return current


T = TypeVar("T")
_QUEUE_STOP = object()


class TopicQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._worker: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._current_task: Optional[asyncio.Task[Any]] = None
        self._cancel_active_requested = False

    def pending(self) -> int:
        return self._queue.qsize()

    def cancel_active(self) -> bool:
        task = self._current_task
        if task is None or task.done():
            return False
        self._cancel_active_requested = True
        task.cancel()
        return True

    def cancel_pending(self) -> int:
        cancelled = 0
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _QUEUE_STOP:
                    requeue_stop = True
                    continue
                work, future = cast(
                    tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]], item
                )
                if not future.done():
                    future.cancel()
                    cancelled += 1
            finally:
                self._queue.task_done()
        if requeue_stop:
            try:
                self._queue.put_nowait(_QUEUE_STOP)
            except asyncio.QueueFull:
                pass
        return cancelled

    async def enqueue(self, work: Callable[[], Awaitable[T]]) -> T:
        if self._closed:
            raise RuntimeError("topic queue is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put((work, future))
        self._ensure_worker()
        return await future

    async def close(self) -> None:
        self._closed = True
        if self._worker is None or self._worker.done():
            return
        await self._queue.put(_QUEUE_STOP)
        await self._worker

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                work, future = cast(
                    tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]], item
                )
                if future.cancelled():
                    continue
                try:
                    self._current_task = asyncio.create_task(work())
                    result: Any = await self._current_task
                except asyncio.CancelledError:
                    if self._cancel_active_requested:
                        self._cancel_active_requested = False
                        if not future.cancelled():
                            future.cancel()
                    else:
                        if (
                            self._current_task is not None
                            and not self._current_task.done()
                        ):
                            self._current_task.cancel()
                        raise
                except Exception as exc:
                    if not future.cancelled():
                        future.set_exception(exc)
                else:
                    if not future.cancelled():
                        future.set_result(result)
            finally:
                self._current_task = None
                self._cancel_active_requested = False
                self._queue.task_done()


@dataclass
class TopicRuntime:
    queue: TopicQueue = dataclasses.field(default_factory=TopicQueue)
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[tuple[str, str]] = None
    pending_request_id: Optional[str] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None
    queued_turn_cancel: Optional[asyncio.Event] = None


class TopicRouter:
    def __init__(self, store: TelegramStateStore) -> None:
        self._store = store
        self._topics: dict[str, TopicRuntime] = {}
        self._scope_cache: dict[str, Optional[str]] = {}

    def runtime_for(self, key: str) -> TopicRuntime:
        runtime = self._topics.get(key)
        if runtime is None:
            runtime = TopicRuntime()
            self._topics[key] = runtime
        return runtime

    async def resolve_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        base_key = topic_key(chat_id, thread_id)
        if base_key not in self._scope_cache:
            scope = await self._store.get_topic_scope(base_key)
            if base_key not in self._scope_cache:
                self._scope_cache[base_key] = scope
        scope = self._scope_cache[base_key]
        if isinstance(scope, str) and scope:
            return topic_key(chat_id, thread_id, scope=scope)
        return base_key

    async def set_topic_scope(
        self, chat_id: int, thread_id: Optional[int], scope: Optional[str]
    ) -> None:
        base_key = topic_key(chat_id, thread_id)
        self._scope_cache[base_key] = scope
        await self._store.set_topic_scope(base_key, scope)

    async def topic_key(
        self, chat_id: int, thread_id: Optional[int], *, scope: Optional[str] = None
    ) -> str:
        if scope is None:
            return await self.resolve_key(chat_id, thread_id)
        return topic_key(chat_id, thread_id, scope=scope)

    async def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return await self._store.get_topic(key)

    async def ensure_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.ensure_topic(key)

    async def update_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        apply: Callable[[TelegramTopicRecord], None],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.update_topic(key, apply)

    async def bind_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        workspace_path: str,
        *,
        repo_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.bind_topic(key, workspace_path, repo_id=repo_id)

    async def set_active_thread(
        self,
        chat_id: int,
        thread_id: Optional[int],
        active_thread_id: Optional[str],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.set_active_thread(key, active_thread_id)

    async def set_approval_mode(
        self,
        chat_id: int,
        thread_id: Optional[int],
        mode: str,
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.set_approval_mode(key, mode)
