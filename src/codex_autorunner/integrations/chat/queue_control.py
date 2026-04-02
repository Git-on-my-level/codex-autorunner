from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ...core.time_utils import now_iso
from ...core.utils import atomic_write, read_json

CHAT_QUEUE_STATE_FILENAME = "chat_queue_state.json"
CHAT_QUEUE_COMMANDS_FILENAME = "chat_queue_commands.json"


def normalize_chat_thread_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized in {"-", "root"}:
        return None
    return normalized


class ChatQueueControlStore:
    """Persist queue snapshots and reset requests for live chat runtimes."""

    def __init__(self, hub_root: Path) -> None:
        state_root = hub_root / ".codex-autorunner"
        self._state_path = state_root / CHAT_QUEUE_STATE_FILENAME
        self._commands_path = state_root / CHAT_QUEUE_COMMANDS_FILENAME

    def read_snapshot(self, conversation_id: str) -> Optional[dict[str, Any]]:
        payload = self._read_state_payload()
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            return None
        entry = conversations.get(str(conversation_id).strip())
        return dict(entry) if isinstance(entry, dict) else None

    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        conversation_id = str(snapshot.get("conversation_id") or "").strip()
        if not conversation_id:
            return
        payload = self._read_state_payload()
        conversations = payload.setdefault("conversations", {})
        if not isinstance(conversations, dict):
            conversations = {}
            payload["conversations"] = conversations

        pending_count = int(snapshot.get("pending_count") or 0)
        active = bool(snapshot.get("active"))
        if pending_count <= 0 and not active:
            conversations.pop(conversation_id, None)
        else:
            stored = dict(snapshot)
            stored["conversation_id"] = conversation_id
            stored["updated_at"] = str(snapshot.get("updated_at") or now_iso())
            conversations[conversation_id] = stored
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
        request = {
            "conversation_id": normalized_conversation,
            "platform": str(platform or "").strip(),
            "chat_id": str(chat_id or "").strip(),
            "thread_id": normalize_chat_thread_id(thread_id),
            "requested_at": now_iso(),
            "requested_by": str(requested_by or "cli").strip() or "cli",
            "reason": str(reason or "").strip() or None,
        }
        requests[normalized_conversation] = request
        self._write_payload(self._commands_path, payload)
        return request

    def take_reset_requests(
        self, *, platform: Optional[str] = None
    ) -> list[dict[str, Any]]:
        payload = self._read_commands_payload()
        requests = payload.get("reset_requests")
        if not isinstance(requests, dict):
            return []

        normalized_platform = str(platform or "").strip().lower() or None
        remaining: dict[str, Any] = {}
        taken: list[dict[str, Any]] = []
        for conversation_id, raw_request in requests.items():
            if not isinstance(raw_request, dict):
                continue
            request_platform = str(raw_request.get("platform") or "").strip().lower()
            if normalized_platform and request_platform != normalized_platform:
                remaining[str(conversation_id)] = raw_request
                continue
            request = dict(raw_request)
            request["conversation_id"] = str(
                request.get("conversation_id") or conversation_id
            ).strip()
            taken.append(request)

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
