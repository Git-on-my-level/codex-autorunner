from __future__ import annotations

import asyncio
import dataclasses
import json
from urllib.parse import quote, unquote
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .state import now_iso, state_lock
from .utils import atomic_write, read_json


STATE_VERSION = 2
TOPIC_ROOT = "root"
APPROVAL_MODE_YOLO = "yolo"
APPROVAL_MODE_SAFE = "safe"
APPROVAL_MODES = {APPROVAL_MODE_YOLO, APPROVAL_MODE_SAFE}


def normalize_approval_mode(mode: Optional[str], *, default: str = APPROVAL_MODE_YOLO) -> str:
    if not isinstance(mode, str):
        return default
    key = mode.strip().lower()
    if key in APPROVAL_MODES:
        return key
    return default


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


@dataclass
class TelegramTopicRecord:
    repo_id: Optional[str] = None
    workspace_path: Optional[str] = None
    active_thread_id: Optional[str] = None
    thread_ids: list[str] = dataclasses.field(default_factory=list)
    model: Optional[str] = None
    effort: Optional[str] = None
    summary: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[Any] = None
    rollout_path: Optional[str] = None
    approval_mode: str = APPROVAL_MODE_YOLO
    last_active_at: Optional[str] = None

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
        active_thread_id = payload.get("active_thread_id") or payload.get("activeThreadId")
        if not isinstance(active_thread_id, str):
            active_thread_id = None
        thread_ids_raw = payload.get("thread_ids") or payload.get("threadIds")
        thread_ids: list[str] = []
        if isinstance(thread_ids_raw, list):
            for item in thread_ids_raw:
                if isinstance(item, str) and item:
                    thread_ids.append(item)
        if not thread_ids and isinstance(active_thread_id, str):
            thread_ids = [active_thread_id]
        model = payload.get("model")
        if not isinstance(model, str):
            model = None
        effort = payload.get("effort") or payload.get("reasoningEffort")
        if not isinstance(effort, str):
            effort = None
        summary = payload.get("summary") or payload.get("summaryMode")
        if not isinstance(summary, str):
            summary = None
        approval_policy = payload.get("approval_policy") or payload.get("approvalPolicy")
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
        return cls(
            repo_id=repo_id,
            workspace_path=workspace_path,
            active_thread_id=active_thread_id,
            thread_ids=thread_ids,
            model=model,
            effort=effort,
            summary=summary,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            rollout_path=rollout_path,
            approval_mode=approval_mode,
            last_active_at=last_active_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "workspace_path": self.workspace_path,
            "active_thread_id": self.active_thread_id,
            "thread_ids": list(self.thread_ids),
            "model": self.model,
            "effort": self.effort,
            "summary": self.summary,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "rollout_path": self.rollout_path,
            "approval_mode": self.approval_mode,
            "last_active_at": self.last_active_at,
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

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "topics": {
                key: record.to_dict() for key, record in self.topics.items()
            },
            "topic_scopes": dict(self.topic_scopes),
            "pending_approvals": {
                key: record.to_dict() for key, record in self.pending_approvals.items()
            },
            "outbox": {
                key: record.to_dict() for key, record in self.outbox.items()
            },
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
        return cls(
            request_id=request_id,
            turn_id=turn_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            prompt=prompt,
            created_at=created_at,
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
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(chat_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if reply_to_message_id is not None and not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        if placeholder_message_id is not None and not isinstance(placeholder_message_id, int):
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
        }


class TelegramStateStore:
    def __init__(
        self, path: Path, *, default_approval_mode: str = APPROVAL_MODE_YOLO
    ) -> None:
        self._path = path
        self._default_approval_mode = normalize_approval_mode(default_approval_mode)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TelegramState:
        with state_lock(self._path):
            return self._load_unlocked()

    def save(self, state: TelegramState) -> None:
        with state_lock(self._path):
            self._save_unlocked(state)

    def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        with state_lock(self._path):
            state = self._load_unlocked()
            return state.topics.get(key)

    def get_topic_scope(self, key: str) -> Optional[str]:
        with state_lock(self._path):
            state = self._load_unlocked()
            return state.topic_scopes.get(key)

    def set_topic_scope(self, key: str, scope: Optional[str]) -> None:
        if not isinstance(key, str) or not key:
            return
        with state_lock(self._path):
            state = self._load_unlocked()
            if isinstance(scope, str) and scope:
                state.topic_scopes[key] = scope
            else:
                state.topic_scopes.pop(key, None)
            self._save_unlocked(state)

    def bind_topic(
        self, key: str, workspace_path: str, *, repo_id: Optional[str] = None
    ) -> TelegramTopicRecord:
        if not isinstance(workspace_path, str) or not workspace_path:
            raise ValueError("workspace_path is required")

        def apply(record: TelegramTopicRecord) -> None:
            # Switching workspaces should restart the app-server thread in the new repo.
            record.workspace_path = workspace_path
            if repo_id is not None:
                record.repo_id = repo_id
            record.active_thread_id = None
            record.thread_ids = []
            record.rollout_path = None

        return self._update_topic(key, apply)

    def set_active_thread(self, key: str, thread_id: Optional[str]) -> TelegramTopicRecord:
        def apply(record: TelegramTopicRecord) -> None:
            record.active_thread_id = thread_id

        return self._update_topic(key, apply)

    def set_approval_mode(self, key: str, mode: str) -> TelegramTopicRecord:
        normalized = normalize_approval_mode(mode, default=self._default_approval_mode)

        def apply(record: TelegramTopicRecord) -> None:
            record.approval_mode = normalized

        return self._update_topic(key, apply)

    def ensure_topic(self, key: str) -> TelegramTopicRecord:
        def apply(_record: TelegramTopicRecord) -> None:
            pass

        return self._update_topic(key, apply)

    def update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        return self._update_topic(key, apply)

    def _load_unlocked(self) -> TelegramState:
        try:
            data = read_json(self._path)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return TelegramState(version=STATE_VERSION)
        version = data.get("version")
        if not isinstance(version, int):
            version = STATE_VERSION
        topics_raw = data.get("topics")
        topics: dict[str, TelegramTopicRecord] = {}
        if isinstance(topics_raw, dict):
            for key, record in topics_raw.items():
                if not isinstance(key, str) or not isinstance(record, dict):
                    continue
                topics[key] = TelegramTopicRecord.from_dict(
                    record, default_approval_mode=self._default_approval_mode
                )
        topic_scopes_raw = data.get("topic_scopes")
        topic_scopes: dict[str, str] = {}
        if isinstance(topic_scopes_raw, dict):
            for key, value in topic_scopes_raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    topic_scopes[key] = value
        approvals_raw = data.get("pending_approvals")
        pending_approvals: dict[str, PendingApprovalRecord] = {}
        if isinstance(approvals_raw, dict):
            for key, record in approvals_raw.items():
                if not isinstance(key, str) or not isinstance(record, dict):
                    continue
                parsed = PendingApprovalRecord.from_dict(record)
                if parsed is None:
                    continue
                pending_approvals[key] = parsed
        outbox_raw = data.get("outbox")
        outbox: dict[str, OutboxRecord] = {}
        if isinstance(outbox_raw, dict):
            for key, record in outbox_raw.items():
                if not isinstance(key, str) or not isinstance(record, dict):
                    continue
                parsed = OutboxRecord.from_dict(record)
                if parsed is None:
                    continue
                outbox[key] = parsed
        return TelegramState(
            version=version,
            topics=topics,
            topic_scopes=topic_scopes,
            pending_approvals=pending_approvals,
            outbox=outbox,
        )

    def _save_unlocked(self, state: TelegramState) -> None:
        atomic_write(self._path, state.to_json())

    def _update_topic(
        self, key: str, apply: Callable[[TelegramTopicRecord], None]
    ) -> TelegramTopicRecord:
        with state_lock(self._path):
            state = self._load_unlocked()
            record = state.topics.get(key)
            if record is None:
                record = TelegramTopicRecord(
                    approval_mode=self._default_approval_mode
                )
            apply(record)
            record.approval_mode = normalize_approval_mode(
                record.approval_mode, default=self._default_approval_mode
            )
            record.last_active_at = now_iso()
            state.topics[key] = record
            self._save_unlocked(state)
            return record

    def upsert_pending_approval(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        with state_lock(self._path):
            state = self._load_unlocked()
            state.pending_approvals[record.request_id] = record
            self._save_unlocked(state)
            return record

    def clear_pending_approval(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            return
        with state_lock(self._path):
            state = self._load_unlocked()
            if request_id in state.pending_approvals:
                state.pending_approvals.pop(request_id, None)
                self._save_unlocked(state)

    def pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> list[PendingApprovalRecord]:
        with state_lock(self._path):
            state = self._load_unlocked()
            pending = [
                record
                for record in state.pending_approvals.values()
                if record.chat_id == chat_id and record.thread_id == thread_id
            ]
        return pending

    def clear_pending_approvals_for_topic(
        self, chat_id: int, thread_id: Optional[int]
    ) -> None:
        with state_lock(self._path):
            state = self._load_unlocked()
            keys = [
                key
                for key, record in state.pending_approvals.items()
                if record.chat_id == chat_id and record.thread_id == thread_id
            ]
            for key in keys:
                state.pending_approvals.pop(key, None)
            if keys:
                self._save_unlocked(state)

    def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        with state_lock(self._path):
            state = self._load_unlocked()
            state.outbox[record.record_id] = record
            self._save_unlocked(state)
            return record

    def update_outbox(self, record: OutboxRecord) -> OutboxRecord:
        with state_lock(self._path):
            state = self._load_unlocked()
            state.outbox[record.record_id] = record
            self._save_unlocked(state)
            return record

    def delete_outbox(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        with state_lock(self._path):
            state = self._load_unlocked()
            if record_id in state.outbox:
                state.outbox.pop(record_id, None)
                self._save_unlocked(state)

    def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        with state_lock(self._path):
            state = self._load_unlocked()
            return state.outbox.get(record_id)

    def list_outbox(self) -> list[OutboxRecord]:
        with state_lock(self._path):
            state = self._load_unlocked()
            records = list(state.outbox.values())
        return sorted(records, key=lambda record: record.created_at or "")


T = TypeVar("T")
_QUEUE_STOP = object()


class TopicQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._worker: Optional[asyncio.Task[None]] = None
        self._closed = False

    def pending(self) -> int:
        return self._queue.qsize()

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
                work, future = item
                if future.cancelled():
                    continue
                try:
                    result = await work()
                except Exception as exc:
                    if not future.cancelled():
                        future.set_exception(exc)
                else:
                    if not future.cancelled():
                        future.set_result(result)
            finally:
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


class TopicRouter:
    def __init__(self, store: TelegramStateStore) -> None:
        self._store = store
        self._topics: dict[str, TopicRuntime] = {}

    def runtime_for(self, key: str) -> TopicRuntime:
        runtime = self._topics.get(key)
        if runtime is None:
            runtime = TopicRuntime()
            self._topics[key] = runtime
        return runtime

    def resolve_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        base_key = topic_key(chat_id, thread_id)
        scope = self._store.get_topic_scope(base_key)
        if isinstance(scope, str) and scope:
            return topic_key(chat_id, thread_id, scope=scope)
        return base_key

    def set_topic_scope(
        self, chat_id: int, thread_id: Optional[int], scope: Optional[str]
    ) -> None:
        base_key = topic_key(chat_id, thread_id)
        self._store.set_topic_scope(base_key, scope)

    def topic_key(
        self, chat_id: int, thread_id: Optional[int], *, scope: Optional[str] = None
    ) -> str:
        if scope is None:
            return self.resolve_key(chat_id, thread_id)
        return topic_key(chat_id, thread_id, scope=scope)

    def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return self._store.get_topic(key)

    def ensure_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id, scope=scope)
        return self._store.ensure_topic(key)

    def update_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        apply: Callable[[TelegramTopicRecord], None],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id, scope=scope)
        return self._store.update_topic(key, apply)

    def bind_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        workspace_path: str,
        *,
        repo_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id, scope=scope)
        return self._store.bind_topic(key, workspace_path, repo_id=repo_id)

    def set_active_thread(
        self,
        chat_id: int,
        thread_id: Optional[int],
        active_thread_id: Optional[str],
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id, scope=scope)
        return self._store.set_active_thread(key, active_thread_id)

    def set_approval_mode(
        self,
        chat_id: int,
        thread_id: Optional[int],
        mode: str,
        *,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id, scope=scope)
        return self._store.set_approval_mode(key, mode)
