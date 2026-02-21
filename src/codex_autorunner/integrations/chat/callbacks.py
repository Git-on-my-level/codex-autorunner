"""Platform-agnostic callback routing models and codec interfaces.

This module belongs to the adapter layer (`integrations/chat`) and defines
logical callback identifiers that are transport-neutral.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

CALLBACK_APPROVAL = "approval"
CALLBACK_QUESTION_OPTION = "question_option"
CALLBACK_QUESTION_DONE = "question_done"
CALLBACK_QUESTION_CUSTOM = "question_custom"
CALLBACK_QUESTION_CANCEL = "question_cancel"
CALLBACK_RESUME = "resume"
CALLBACK_BIND = "bind"
CALLBACK_AGENT = "agent"
CALLBACK_MODEL = "model"
CALLBACK_EFFORT = "effort"
CALLBACK_UPDATE = "update"
CALLBACK_UPDATE_CONFIRM = "update_confirm"
CALLBACK_REVIEW_COMMIT = "review_commit"
CALLBACK_CANCEL = "cancel"
CALLBACK_COMPACT = "compact"
CALLBACK_PAGE = "page"
CALLBACK_FLOW = "flow"
CALLBACK_FLOW_RUN = "flow_run"


@dataclass(frozen=True)
class LogicalCallback:
    """Transport-neutral callback id plus structured payload."""

    callback_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class CallbackCodec(Protocol):
    """Codec interface for mapping logical callbacks to platform payloads."""

    def decode(self, platform_payload: Optional[str]) -> Optional[LogicalCallback]:
        """Decode platform-specific payload to logical callback."""

    def encode(self, callback: LogicalCallback) -> str:
        """Encode logical callback into platform-specific payload."""


def encode_logical_callback(callback: LogicalCallback) -> str:
    """Serialize a logical callback for normalized chat interaction payloads."""

    return json.dumps(
        {"callback_id": callback.callback_id, "payload": callback.payload},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_logical_callback(payload: Optional[str]) -> Optional[LogicalCallback]:
    """Deserialize a normalized logical callback payload."""

    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    callback_id = data.get("callback_id")
    callback_payload = data.get("payload")
    if not isinstance(callback_id, str):
        return None
    if not isinstance(callback_payload, Mapping):
        return None
    return LogicalCallback(callback_id=callback_id, payload=dict(callback_payload))
