from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .state_types import TelegramTopicRecord, topic_key
from .topic_queue import TopicRuntime

if TYPE_CHECKING:
    from .state import TelegramStateStore


class TopicRouter:
    def __init__(self, store: TelegramStateStore) -> None:
        self._store: TelegramStateStore = store
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
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> TelegramTopicRecord:
        key = await self.topic_key(chat_id, thread_id, scope=scope)
        return await self._store.bind_topic(
            key,
            workspace_path,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_id=workspace_id,
        )

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
