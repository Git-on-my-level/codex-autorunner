from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .state import now_iso, state_lock
from .utils import atomic_write, read_json


STATE_VERSION = 1
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


def topic_key(chat_id: int, thread_id: Optional[int]) -> str:
    if not isinstance(chat_id, int):
        raise TypeError("chat_id must be int")
    suffix = str(thread_id) if thread_id is not None else TOPIC_ROOT
    return f"{chat_id}:{suffix}"


def parse_topic_key(key: str) -> tuple[int, Optional[int]]:
    chat_raw, sep, thread_raw = key.partition(":")
    if not sep or not chat_raw or not thread_raw:
        raise ValueError("invalid topic key")
    try:
        chat_id = int(chat_raw)
    except ValueError as exc:
        raise ValueError("invalid chat id in topic key") from exc
    if thread_raw == TOPIC_ROOT:
        return chat_id, None
    try:
        thread_id = int(thread_raw)
    except ValueError as exc:
        raise ValueError("invalid thread id in topic key") from exc
    return chat_id, thread_id


@dataclass
class TelegramTopicRecord:
    repo_id: Optional[str] = None
    workspace_path: Optional[str] = None
    active_thread_id: Optional[str] = None
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
            approval_mode=approval_mode,
            last_active_at=last_active_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "workspace_path": self.workspace_path,
            "active_thread_id": self.active_thread_id,
            "approval_mode": self.approval_mode,
            "last_active_at": self.last_active_at,
        }


@dataclass
class TelegramState:
    version: int = STATE_VERSION
    topics: dict[str, TelegramTopicRecord] = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "topics": {
                key: record.to_dict() for key, record in self.topics.items()
            },
        }
        return json.dumps(payload, indent=2) + "\n"


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

    def bind_topic(
        self, key: str, workspace_path: str, *, repo_id: Optional[str] = None
    ) -> TelegramTopicRecord:
        if not isinstance(workspace_path, str) or not workspace_path:
            raise ValueError("workspace_path is required")

        def apply(record: TelegramTopicRecord) -> None:
            record.workspace_path = workspace_path
            if repo_id is not None:
                record.repo_id = repo_id

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
        return TelegramState(version=version, topics=topics)

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
    pending_request_id: Optional[str] = None
    interrupt_requested: bool = False


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

    def topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return topic_key(chat_id, thread_id)

    def get_topic(self, key: str) -> Optional[TelegramTopicRecord]:
        return self._store.get_topic(key)

    def bind_topic(
        self,
        chat_id: int,
        thread_id: Optional[int],
        workspace_path: str,
        *,
        repo_id: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id)
        return self._store.bind_topic(key, workspace_path, repo_id=repo_id)

    def set_active_thread(
        self,
        chat_id: int,
        thread_id: Optional[int],
        active_thread_id: Optional[str],
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id)
        return self._store.set_active_thread(key, active_thread_id)

    def set_approval_mode(
        self,
        chat_id: int,
        thread_id: Optional[int],
        mode: str,
    ) -> TelegramTopicRecord:
        key = self.topic_key(chat_id, thread_id)
        return self._store.set_approval_mode(key, mode)
