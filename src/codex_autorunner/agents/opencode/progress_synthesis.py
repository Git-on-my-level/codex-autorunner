"""OpenCode harness progress synthesis and descendant-session tracking.

Owns silent-turn heartbeat generation, message-snapshot polling, descendant-
session lineage discovery, and synthetic progress-event creation.  The harness
delegates to these helpers instead of embedding the logic inline in
``start_turn``, ``start_review``, and ``wait_for_turn``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from ...core.logging_utils import log_event
from ...core.orchestration.stream_text_merge import merge_assistant_stream_text
from .event_fields import (
    extract_message_id as _extract_message_id,
)
from .event_fields import (
    extract_part_id as _extract_part_id,
)
from .event_fields import (
    extract_part_message_id as _extract_part_message_id,
)
from .event_fields import (
    extract_part_type as _extract_part_type,
)
from .protocol_payload import (
    extract_completed_text,
    extract_delta_text,
    extract_session_id,
    parse_message_response,
)

SILENT_TURN_HEARTBEAT_SECONDS = 20.0
SILENT_TURN_PROGRESS_POLL_SECONDS = 1.0

_logger = logging.getLogger(__name__)


def wrap_runtime_raw_event(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"message": {"method": method, "params": params}}


def with_session_id(payload: Any, conversation_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"sessionID": conversation_id}
    normalized = dict(payload)
    if not extract_session_id(normalized, allow_fallback_id=True):
        normalized["sessionID"] = conversation_id
    return normalized


def extract_parent_session_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    containers: list[Any] = [payload]
    properties = payload.get("properties")
    if isinstance(properties, dict):
        containers.append(properties)
        properties_info = properties.get("info")
        if isinstance(properties_info, dict):
            containers.append(properties_info)
    info = payload.get("info")
    if isinstance(info, dict):
        containers.append(info)
    session = payload.get("session")
    if isinstance(session, dict):
        containers.append(session)
    item = payload.get("item")
    if isinstance(item, dict):
        containers.append(item)

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("parentID", "parentId", "parent_id"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def descendant_text_progress_key(method: str, payload: dict[str, Any]) -> str:
    item = payload.get("item")
    if isinstance(item, dict):
        item_id = item.get("id") or item.get("itemId")
        if isinstance(item_id, str) and item_id:
            return f"item:{item_id}"
    item_id = payload.get("itemId")
    if isinstance(item_id, str) and item_id:
        return f"item:{item_id}"
    message_id = _extract_part_message_id(payload) or _extract_message_id(payload)
    if isinstance(message_id, str) and message_id:
        return f"message:{message_id}"
    part_id = _extract_part_id(payload)
    if isinstance(part_id, str) and part_id:
        return f"part:{part_id}"
    return f"stream:{method}"


def synthetic_descendant_reasoning_event(
    *,
    session_id: str,
    logical_id: str,
    text: str,
) -> dict[str, Any]:
    synthetic_message_id = f"descendant-progress:{session_id}:{logical_id}:message"
    synthetic_part_id = f"descendant-progress:{session_id}:{logical_id}:part"
    return wrap_runtime_raw_event(
        "message.part.updated",
        {
            "sessionID": session_id,
            "messageID": synthetic_message_id,
            "properties": {
                "messageID": synthetic_message_id,
                "info": {
                    "id": synthetic_message_id,
                    "role": "assistant",
                },
                "part": {
                    "id": synthetic_part_id,
                    "type": "reasoning",
                    "text": text,
                    "messageID": synthetic_message_id,
                    "sessionID": session_id,
                },
            },
        },
    )


def synthetic_command_result_events(
    conversation_id: str, payload: Any
) -> list[dict[str, Any]]:
    normalized = with_session_id(payload, conversation_id)
    parsed = parse_message_response(normalized)
    events: list[dict[str, Any]] = []
    if parsed.text:
        events.append(wrap_runtime_raw_event("message.completed", normalized))
    elif parsed.error:
        events.append(
            wrap_runtime_raw_event(
                "error",
                {
                    "sessionID": conversation_id,
                    "message": parsed.error,
                },
            )
        )
    if events:
        events.append(
            wrap_runtime_raw_event(
                "session.idle",
                {
                    "sessionID": conversation_id,
                },
            )
        )
    return events


def synthetic_message_snapshot_events(
    conversation_id: str,
    payload: Any,
    *,
    message_roles_seen: set[str],
    part_signatures: dict[str, str],
) -> list[dict[str, Any]]:
    messages_raw: Any = payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            messages_raw = data
    if not isinstance(messages_raw, list):
        return []

    raw_events: list[dict[str, Any]] = []
    for entry in messages_raw:
        if not isinstance(entry, dict):
            continue
        info_raw = entry.get("info")
        info = info_raw if isinstance(info_raw, dict) else {}
        message_id = info.get("id") or entry.get("id")
        role = info.get("role") or entry.get("role")
        if not isinstance(message_id, str) or not message_id:
            continue
        if role != "assistant":
            continue
        if message_id not in message_roles_seen:
            message_roles_seen.add(message_id)
            raw_events.append(
                wrap_runtime_raw_event(
                    "message.updated",
                    {
                        "sessionID": conversation_id,
                        "info": {"id": message_id, "role": "assistant"},
                    },
                )
            )
        parts = entry.get("parts")
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip().lower()
            if part_type not in {"reasoning", "tool", "patch", "usage"}:
                continue
            normalized_part = dict(part)
            normalized_part.setdefault("messageID", message_id)
            normalized_part.setdefault("sessionID", conversation_id)
            part_id = (
                normalized_part.get("id")
                or normalized_part.get("callID")
                or f"{part_type}:{message_id}:{index}"
            )
            signature_key = str(part_id)
            signature = json.dumps(normalized_part, sort_keys=True, ensure_ascii=True)
            if part_signatures.get(signature_key) == signature:
                continue
            part_signatures[signature_key] = signature
            raw_events.append(
                wrap_runtime_raw_event(
                    "message.part.updated",
                    {
                        "sessionID": conversation_id,
                        "messageID": message_id,
                        "properties": {
                            "messageID": message_id,
                            "info": {"id": message_id, "role": "assistant"},
                            "part": normalized_part,
                        },
                    },
                )
            )
    return raw_events


def progress_event_shape_hint(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    hints: list[str] = []
    keys = sorted(str(key) for key in payload.keys())
    if keys:
        hints.append(f"keys={','.join(keys[:6])}")
    properties = payload.get("properties")
    if isinstance(properties, dict):
        property_keys = sorted(str(key) for key in properties.keys())
        if property_keys:
            hints.append(f"properties={','.join(property_keys[:6])}")
    item = payload.get("item")
    if not isinstance(item, dict) and isinstance(properties, dict):
        nested_item = properties.get("item")
        if isinstance(nested_item, dict):
            item = nested_item
    if isinstance(item, dict):
        item_keys = sorted(str(key) for key in item.keys())
        if item_keys:
            hints.append(f"item={','.join(item_keys[:6])}")
    return " ".join(hints) or None


class DescendantSessionTracker:
    """Tracks descendant-session lineage and synthesizes progress events.

    Encapsulates parent-chain walking, session caching, and descendant-specific
    event synthesis so the harness does not inline this logic inside
    ``wait_for_turn``.
    """

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.watched_session_ids: set[str] = {conversation_id}
        self.session_parent_cache: dict[str, Optional[str]] = {conversation_id: None}
        self.non_descendant_session_ids: set[str] = set()
        self.text_buffers: dict[str, str] = {}
        self.part_types: dict[str, str] = {}

    async def session_belongs_to_turn(
        self, session_id: Optional[str], client: Any
    ) -> bool:
        if not isinstance(session_id, str) or not session_id:
            return False
        if session_id in self.watched_session_ids:
            return True
        if session_id in self.non_descendant_session_ids:
            return False

        current: Optional[str] = session_id
        visited: list[str] = []
        visited_set: set[str] = set()

        while current:
            if current in self.watched_session_ids:
                lineage_parent = current
                for descendant in reversed(visited):
                    self.watched_session_ids.add(descendant)
                    self.session_parent_cache[descendant] = lineage_parent
                    lineage_parent = descendant
                return True
            if current in self.non_descendant_session_ids or current in visited_set:
                break
            visited.append(current)
            visited_set.add(current)

            parent_session_id = self.session_parent_cache.get(current)
            if parent_session_id is None and current not in self.session_parent_cache:
                try:
                    session_payload = await client.get_session(current)
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                    httpx.HTTPError,
                ):
                    _logger.debug(
                        "get_session for lineage lookup failed", exc_info=True
                    )
                    return False
                parent_session_id = extract_parent_session_id(session_payload)
                self.session_parent_cache[current] = parent_session_id
            current = parent_session_id

        self.non_descendant_session_ids.update(visited)
        return False

    async def maybe_track_descendant_session(
        self, payload: dict[str, Any], client: Any
    ) -> None:
        event_session_id = extract_session_id(payload)
        if not isinstance(event_session_id, str) or not event_session_id:
            return
        if event_session_id in self.watched_session_ids:
            parent_session_id = extract_parent_session_id(payload)
            if parent_session_id is not None:
                self.session_parent_cache[event_session_id] = parent_session_id
            return

        parent_session_id = extract_parent_session_id(payload)
        if isinstance(parent_session_id, str) and parent_session_id:
            self.session_parent_cache[event_session_id] = parent_session_id
            if (
                parent_session_id in self.watched_session_ids
                or await self.session_belongs_to_turn(parent_session_id, client)
            ):
                self.watched_session_ids.add(event_session_id)
                self.non_descendant_session_ids.discard(event_session_id)
                log_event(
                    _logger,
                    logging.INFO,
                    "opencode.progress_session.tracked",
                    conversation_id=self.conversation_id,
                    session_id=event_session_id,
                    parent_session_id=parent_session_id,
                    source="event_parent",
                )
            return

        if await self.session_belongs_to_turn(event_session_id, client):
            log_event(
                _logger,
                logging.INFO,
                "opencode.progress_session.tracked",
                conversation_id=self.conversation_id,
                session_id=event_session_id,
                parent_session_id=self.session_parent_cache.get(event_session_id),
                source="session_lookup",
            )

    def descendant_progress_events(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        session_id: str,
    ) -> list[dict[str, Any]]:
        if method in {"message.part.updated", "message.part.delta"}:
            part_type = _extract_part_type(payload, part_types=self.part_types)
            if part_type in {None, "", "text"}:
                text = extract_delta_text(payload) or extract_completed_text(payload)
                if not text:
                    return []
                progress_key = (
                    f"{session_id}:{descendant_text_progress_key(method, payload)}"
                )
                accumulated_text = merge_assistant_stream_text(
                    self.text_buffers.get(progress_key, ""),
                    text,
                )
                self.text_buffers[progress_key] = accumulated_text
                return [
                    synthetic_descendant_reasoning_event(
                        session_id=session_id,
                        logical_id=progress_key,
                        text=accumulated_text,
                    )
                ]
            return [wrap_runtime_raw_event(method, payload)]

        if method in {
            "message.delta",
            "message.updated",
            "message.completed",
            "item/agentMessage/delta",
        }:
            text = extract_delta_text(payload) or extract_completed_text(payload)
            if not text:
                return []
            progress_key = (
                f"{session_id}:{descendant_text_progress_key(method, payload)}"
            )
            accumulated_text = merge_assistant_stream_text(
                self.text_buffers.get(progress_key, ""),
                text,
            )
            self.text_buffers[progress_key] = accumulated_text
            return [
                synthetic_descendant_reasoning_event(
                    session_id=session_id,
                    logical_id=progress_key,
                    text=accumulated_text,
                )
            ]

        if method == "item/completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = extract_completed_text(payload)
                if not text:
                    return []
                progress_key = (
                    f"{session_id}:{descendant_text_progress_key(method, payload)}"
                )
                accumulated_text = merge_assistant_stream_text(
                    self.text_buffers.get(progress_key, ""),
                    text,
                )
                self.text_buffers[progress_key] = accumulated_text
                return [
                    synthetic_descendant_reasoning_event(
                        session_id=session_id,
                        logical_id=progress_key,
                        text=accumulated_text,
                    )
                ]
            return [wrap_runtime_raw_event(method, payload)]

        if (
            method
            in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/summaryPartAdded",
                "item/reasoning/textDelta",
                "item/toolCall/start",
                "item/toolCall/end",
            }
            or "outputdelta" in method.lower()
        ):
            return [wrap_runtime_raw_event(method, payload)]

        return []


def start_silent_turn_progress(
    command_task: asyncio.Task[Any],
    pending: Any,
    conversation_id: str,
    client: Any,
) -> tuple[asyncio.Task[None], asyncio.Task[None]]:
    """Create heartbeat and message-polling tasks for a silent turn.

    Returns ``(heartbeat_task, message_progress_task)`` so the caller can
    assign them to the pending config and cancel them later.
    """

    async def _emit_busy_heartbeats() -> None:
        try:
            while not command_task.done():
                if pending.pre_connected_event_seen.is_set():
                    break
                raw_event = wrap_runtime_raw_event(
                    "session.status",
                    {
                        "sessionID": conversation_id,
                        "status": {"type": "busy"},
                    },
                )
                pending.synthetic_raw_events.append(raw_event)
                await pending.event_buffer.append(raw_event)
                await asyncio.sleep(SILENT_TURN_HEARTBEAT_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _poll_message_progress() -> None:
        try:
            while not command_task.done():
                if pending.pre_connected_event_seen.is_set():
                    break
                try:
                    payload = await client.list_messages(conversation_id, limit=10)
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                    httpx.HTTPError,
                ):
                    _logger.debug("list_messages poll failed", exc_info=True)
                    await asyncio.sleep(SILENT_TURN_PROGRESS_POLL_SECONDS)
                    continue
                if pending.pre_connected_event_seen.is_set():
                    break
                for raw_event in synthetic_message_snapshot_events(
                    conversation_id,
                    payload,
                    message_roles_seen=pending.message_progress_roles_seen,
                    part_signatures=pending.message_progress_part_signatures,
                ):
                    pending.synthetic_raw_events.append(raw_event)
                    await pending.event_buffer.append(raw_event)
                await asyncio.sleep(SILENT_TURN_PROGRESS_POLL_SECONDS)
        except asyncio.CancelledError:
            raise

    heartbeat_task = asyncio.create_task(_emit_busy_heartbeats())
    message_progress_task = asyncio.create_task(_poll_message_progress())
    return heartbeat_task, message_progress_task
