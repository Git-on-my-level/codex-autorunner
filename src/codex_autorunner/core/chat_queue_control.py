from __future__ import annotations

import inspect
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .time_utils import now_iso
from .utils import atomic_write, read_json

CHAT_QUEUE_STATE_FILENAME = "chat_queue_state.json"
CHAT_QUEUE_COMMANDS_FILENAME = "chat_queue_commands.json"


def normalize_chat_thread_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized in {"-", "root"}:
        return None
    return normalized


@dataclass(frozen=True)
class ChatQueueItem:
    item_id: str
    preview: Optional[str] = None

    @classmethod
    def from_mapping(cls, payload: Any) -> Optional["ChatQueueItem"]:
        if not isinstance(payload, dict):
            return None
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            return None
        preview_raw = payload.get("preview")
        preview = str(preview_raw).strip() if preview_raw is not None else None
        return cls(item_id=item_id, preview=preview or None)

    def to_dict(self) -> dict[str, str]:
        payload = {"item_id": self.item_id}
        if self.preview:
            payload["preview"] = self.preview
        return payload


@dataclass(frozen=True)
class ChatQueueSnapshot:
    conversation_id: str
    platform: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    pending_count: int = 0
    pending_items: tuple[ChatQueueItem, ...] = ()
    active: bool = False
    active_update_id: Optional[str] = None
    active_started_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_mapping(cls, payload: Any) -> Optional["ChatQueueSnapshot"]:
        if not isinstance(payload, dict):
            return None
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            return None
        items = tuple(
            item
            for item in (
                ChatQueueItem.from_mapping(raw)
                for raw in payload.get("pending_items") or []
            )
            if item is not None
        )
        return cls(
            conversation_id=conversation_id,
            platform=_optional_str(payload.get("platform")),
            chat_id=_optional_str(payload.get("chat_id")),
            thread_id=normalize_chat_thread_id(payload.get("thread_id")),
            pending_count=int(payload.get("pending_count") or len(items)),
            pending_items=items,
            active=bool(payload.get("active")),
            active_update_id=_optional_str(payload.get("active_update_id")),
            active_started_at=_optional_str(payload.get("active_started_at")),
            updated_at=_optional_str(payload.get("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "pending_count": self.pending_count,
            "pending_items": [item.to_dict() for item in self.pending_items],
            "active": self.active,
            "active_update_id": self.active_update_id,
            "active_started_at": self.active_started_at,
            "updated_at": self.updated_at or now_iso(),
        }


@dataclass(frozen=True)
class ChatQueueResetRequest:
    conversation_id: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    requested_at: str
    requested_by: str = "cli"
    reason: Optional[str] = None

    @classmethod
    def from_mapping(
        cls, conversation_id: str, payload: Any
    ) -> Optional["ChatQueueResetRequest"]:
        if not isinstance(payload, dict):
            return None
        normalized_conversation = str(
            payload.get("conversation_id") or conversation_id
        ).strip()
        if not normalized_conversation:
            return None
        return cls(
            conversation_id=normalized_conversation,
            platform=str(payload.get("platform") or "").strip(),
            chat_id=str(payload.get("chat_id") or "").strip(),
            thread_id=normalize_chat_thread_id(payload.get("thread_id")),
            requested_at=str(payload.get("requested_at") or now_iso()),
            requested_by=str(payload.get("requested_by") or "cli").strip() or "cli",
            reason=_optional_str(payload.get("reason")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatQueueControlResult:
    conversation_id: str
    matched: bool
    cancelled_pending: int = 0
    cancelled_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChatQueueRuntime(Protocol):
    async def queue_status(self, conversation_id: str) -> dict[str, Any]: ...

    async def cancel_pending_message(
        self, conversation_id: str, message_id: str
    ) -> bool: ...

    async def promote_pending_message(
        self, conversation_id: str, message_id: str
    ) -> bool: ...

    async def force_reset(self, conversation_id: str) -> dict[str, Any]: ...


class ChatQueueControlStore:
    """Durable command/read-model backing for live chat queue control."""

    def __init__(self, hub_root: Path) -> None:
        state_root = hub_root / ".codex-autorunner"
        self._state_path = state_root / CHAT_QUEUE_STATE_FILENAME
        self._commands_path = state_root / CHAT_QUEUE_COMMANDS_FILENAME

    def read_snapshot(self, conversation_id: str) -> Optional[dict[str, Any]]:
        snapshot = self.read_typed_snapshot(conversation_id)
        return snapshot.to_dict() if snapshot is not None else None

    def read_typed_snapshot(self, conversation_id: str) -> Optional[ChatQueueSnapshot]:
        payload = self._read_state_payload()
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            return None
        entry = conversations.get(str(conversation_id).strip())
        return ChatQueueSnapshot.from_mapping(entry)

    def record_snapshot(self, snapshot: dict[str, Any] | ChatQueueSnapshot) -> None:
        typed_snapshot = (
            snapshot
            if isinstance(snapshot, ChatQueueSnapshot)
            else ChatQueueSnapshot.from_mapping(snapshot)
        )
        if typed_snapshot is None:
            return
        payload = self._read_state_payload()
        conversations = payload.setdefault("conversations", {})
        if not isinstance(conversations, dict):
            conversations = {}
            payload["conversations"] = conversations

        if typed_snapshot.pending_count <= 0 and not typed_snapshot.active:
            conversations.pop(typed_snapshot.conversation_id, None)
        else:
            conversations[typed_snapshot.conversation_id] = typed_snapshot.to_dict()
        self._write_payload(self._state_path, payload)

    def clear_snapshot(self, conversation_id: str) -> None:
        payload = self._read_state_payload()
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            return
        conversations.pop(str(conversation_id).strip(), None)
        self._write_payload(self._state_path, payload)

    def request_reset(
        self,
        *,
        conversation_id: str,
        platform: str,
        chat_id: str,
        thread_id: Optional[str],
        requested_by: str = "cli",
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_conversation = str(conversation_id or "").strip()
        if not normalized_conversation:
            raise ValueError("conversation_id is required")
        payload = self._read_commands_payload()
        requests = payload.setdefault("reset_requests", {})
        if not isinstance(requests, dict):
            requests = {}
            payload["reset_requests"] = requests
        request = ChatQueueResetRequest(
            conversation_id=normalized_conversation,
            platform=str(platform or "").strip(),
            chat_id=str(chat_id or "").strip(),
            thread_id=normalize_chat_thread_id(thread_id),
            requested_at=now_iso(),
            requested_by=str(requested_by or "cli").strip() or "cli",
            reason=str(reason or "").strip() or None,
        )
        requests[normalized_conversation] = request.to_dict()
        self._write_payload(self._commands_path, payload)
        return request.to_dict()

    def has_reset_requests(self, *, platform: Optional[str] = None) -> bool:
        try:
            stat_result = os.stat(self._commands_path)
        except OSError:
            return False
        if stat_result.st_size < 3:
            return False
        payload = self._read_commands_payload()
        requests = payload.get("reset_requests")
        if not isinstance(requests, dict) or not requests:
            return False
        normalized_platform = str(platform or "").strip().lower() or None
        if normalized_platform is None:
            return True
        return any(
            str(v.get("platform") or "").strip().lower() == normalized_platform
            for v in requests.values()
            if isinstance(v, dict)
        )

    def take_reset_requests(
        self, *, platform: Optional[str] = None
    ) -> list[dict[str, Any]]:
        return [
            request.to_dict()
            for request in self.take_typed_reset_requests(platform=platform)
        ]

    def take_typed_reset_requests(
        self, *, platform: Optional[str] = None
    ) -> list[ChatQueueResetRequest]:
        payload = self._read_commands_payload()
        requests = payload.get("reset_requests")
        if not isinstance(requests, dict) or not requests:
            return []

        normalized_platform = str(platform or "").strip().lower() or None
        remaining: dict[str, Any] = {}
        taken: list[ChatQueueResetRequest] = []
        for conversation_id, raw_request in requests.items():
            request = ChatQueueResetRequest.from_mapping(
                str(conversation_id), raw_request
            )
            if request is None:
                continue
            if (
                normalized_platform
                and request.platform.strip().lower() != normalized_platform
            ):
                remaining[str(conversation_id)] = raw_request
                continue
            taken.append(request)

        if not taken:
            return []

        payload["reset_requests"] = remaining
        self._write_payload(self._commands_path, payload)
        return taken

    def _read_state_payload(self) -> dict[str, Any]:
        payload = read_json(self._state_path)
        return payload if isinstance(payload, dict) else {"conversations": {}}

    def _read_commands_payload(self) -> dict[str, Any]:
        payload = read_json(self._commands_path)
        return payload if isinstance(payload, dict) else {"reset_requests": {}}

    def _write_payload(self, path: Path, payload: dict[str, Any]) -> None:
        atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


class ChatQueueControlPlane:
    """Typed command/read-model facade for adapter queue controls."""

    def __init__(
        self,
        store: ChatQueueControlStore,
        runtime: Optional[object] = None,
    ) -> None:
        self._store = store
        self._runtime = runtime

    def read_snapshot(self, conversation_id: str) -> Optional[ChatQueueSnapshot]:
        return self._store.read_typed_snapshot(conversation_id)

    def record_snapshot(self, snapshot: ChatQueueSnapshot | dict[str, Any]) -> None:
        self._store.record_snapshot(snapshot)

    def clear_snapshot(self, conversation_id: str) -> None:
        self._store.clear_snapshot(conversation_id)

    def request_reset(
        self,
        *,
        conversation_id: str,
        platform: str,
        chat_id: str,
        thread_id: Optional[str],
        requested_by: str = "cli",
        reason: Optional[str] = None,
    ) -> ChatQueueResetRequest:
        payload = self._store.request_reset(
            conversation_id=conversation_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            requested_by=requested_by,
            reason=reason,
        )
        request = ChatQueueResetRequest.from_mapping(conversation_id, payload)
        if request is None:
            raise RuntimeError("stored reset request could not be read back")
        return request

    def has_reset_requests(self, *, platform: Optional[str] = None) -> bool:
        return self._store.has_reset_requests(platform=platform)

    def take_reset_requests(
        self, *, platform: Optional[str] = None
    ) -> list[ChatQueueResetRequest]:
        return self._store.take_typed_reset_requests(platform=platform)

    async def queue_status(self, conversation_id: str) -> Optional[ChatQueueSnapshot]:
        status = await self._call_runtime(
            "queue_status",
            conversation_id,
        )
        if isinstance(status, dict) and not status.get("conversation_id"):
            status = {**status, "conversation_id": conversation_id}
        if status is not None:
            return ChatQueueSnapshot.from_mapping(status)
        return self.read_snapshot(conversation_id)

    async def cancel_pending_item(self, conversation_id: str, item_id: str) -> bool:
        if not _valid_item_id(item_id):
            return False
        result = await self._call_runtime(
            "cancel_pending_message",
            conversation_id,
            item_id,
        )
        if result is None:
            result = await self._call_runtime("cancel_pending_item", item_id)
        return bool(result)

    async def promote_pending_item(self, conversation_id: str, item_id: str) -> bool:
        if not _valid_item_id(item_id):
            return False
        result = await self._call_runtime(
            "promote_pending_message",
            conversation_id,
            item_id,
        )
        if result is None:
            result = await self._call_runtime("promote_pending_item", item_id)
        return bool(result)

    async def force_reset(self, conversation_id: str) -> ChatQueueControlResult:
        result = await self._call_runtime("force_reset", conversation_id)
        if isinstance(result, dict):
            return ChatQueueControlResult(
                conversation_id=conversation_id,
                matched=True,
                cancelled_pending=int(result.get("cancelled_pending") or 0),
                cancelled_active=bool(result.get("cancelled_active")),
            )
        return ChatQueueControlResult(conversation_id=conversation_id, matched=False)

    async def _call_runtime(self, method_name: str, *args: object) -> Any:
        runtime = self._runtime
        if runtime is None:
            return None
        method = getattr(runtime, method_name, None)
        if not callable(method):
            return None
        result = method(*args)
        if inspect.isawaitable(result):
            return await result
        return result


def _valid_item_id(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
