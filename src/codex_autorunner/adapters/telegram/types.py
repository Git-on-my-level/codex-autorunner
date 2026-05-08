from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Optional, Union

from ..app_server.client import ApprovalDecision
from .helpers import ModelOption


@dataclass
class PendingApproval:
    request_id: str
    turn_id: str
    codex_thread_id: Optional[str]
    chat_id: int
    thread_id: Optional[int]
    topic_key: Optional[str]
    message_id: Optional[int]
    created_at: str
    future: asyncio.Future[ApprovalDecision]


@dataclass
class PendingQuestion:
    request_id: str
    turn_id: str
    codex_thread_id: Optional[str]
    chat_id: int
    thread_id: Optional[int]
    topic_key: Optional[str]
    requester_user_id: Optional[str]
    message_id: Optional[int]
    created_at: str
    question_index: int
    prompt: str
    options: list[str]
    future: asyncio.Future[Union[list[int], str, None]]
    multiple: bool = False
    custom: bool = True
    selected_indices: set[int] = field(default_factory=set)
    awaiting_custom_input: bool = False


@dataclass
class TurnContext:
    topic_key: str
    chat_id: int
    thread_id: Optional[int]
    codex_thread_id: Optional[str]
    reply_to_message_id: Optional[int]
    placeholder_message_id: Optional[int] = None
    placeholder_reused: bool = False


@dataclass
class CompactState:
    summary_text: str
    display_text: str
    message_id: int
    created_at: str
    requester_user_id: Optional[str] = None


@dataclass(frozen=True)
class UpdateConfirmState:
    target: Optional[str] = None
    requester_user_id: Optional[str] = None


@dataclass(frozen=True)
class ModelPendingState:
    option: ModelOption
    requester_user_id: Optional[str] = None


def _coerce_optional_telegram_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True)
class TelegramNoticeContext:
    chat_id: int
    thread_id: Optional[int] = None
    reply_to: Optional[int] = None

    def to_payload(self) -> dict[str, int]:
        payload: dict[str, int] = {"chat_id": self.chat_id}
        if self.thread_id is not None:
            payload["thread_id"] = self.thread_id
        if self.reply_to is not None:
            payload["reply_to"] = self.reply_to
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> Optional["TelegramNoticeContext"]:
        if not isinstance(payload, dict):
            return None
        chat_id = _coerce_optional_telegram_int(payload.get("chat_id"))
        if chat_id is None:
            return None
        return cls(
            chat_id=chat_id,
            thread_id=_coerce_optional_telegram_int(payload.get("thread_id")),
            reply_to=_coerce_optional_telegram_int(payload.get("reply_to")),
        )


@dataclass(frozen=True)
class CompactStatusState:
    status: str
    message: str
    at: float
    chat_id: Optional[int] = None
    thread_id: Optional[int] = None
    message_id: Optional[int] = None
    display_text: Optional[str] = None
    error_detail: Optional[str] = None
    started_at: Optional[float] = None
    notify_sent_at: Optional[float] = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "message": self.message,
            "at": self.at,
        }
        if self.chat_id is not None:
            payload["chat_id"] = self.chat_id
        if self.thread_id is not None:
            payload["thread_id"] = self.thread_id
        if self.message_id is not None:
            payload["message_id"] = self.message_id
        if self.display_text is not None:
            payload["display_text"] = self.display_text
        if self.error_detail is not None:
            payload["error_detail"] = self.error_detail
        if self.started_at is not None:
            payload["started_at"] = self.started_at
        if self.notify_sent_at is not None:
            payload["notify_sent_at"] = self.notify_sent_at
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> Optional["CompactStatusState"]:
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        message = payload.get("message")
        at = payload.get("at")
        if not isinstance(status, str) or not isinstance(message, str):
            return None
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        error_detail = payload.get("error_detail")
        display_text = payload.get("display_text")
        if display_text is not None and not isinstance(display_text, str):
            display_text = None
        if error_detail is not None and not isinstance(error_detail, str):
            error_detail = None
        started_at = payload.get("started_at")
        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            started_at = None
        notify_sent_at = payload.get("notify_sent_at")
        if isinstance(notify_sent_at, bool) or not isinstance(
            notify_sent_at, (int, float)
        ):
            notify_sent_at = None
        return cls(
            status=status,
            message=message,
            at=float(at),
            chat_id=_coerce_optional_telegram_int(payload.get("chat_id")),
            thread_id=_coerce_optional_telegram_int(payload.get("thread_id")),
            message_id=_coerce_optional_telegram_int(payload.get("message_id")),
            display_text=display_text,
            error_detail=error_detail,
            started_at=float(started_at) if started_at is not None else None,
            notify_sent_at=(
                float(notify_sent_at) if notify_sent_at is not None else None
            ),
        )

    def with_notify_sent_at(self, at: float) -> "CompactStatusState":
        return dataclasses.replace(self, notify_sent_at=at)


@dataclass
class SelectionState:
    items: list[tuple[str, str]]
    page: int = 0
    button_labels: Optional[dict[str, str]] = None
    repo_id: Optional[str] = None
    requester_user_id: Optional[str] = None


@dataclass
class ReviewCommitSelectionState(SelectionState):
    delivery: str = "inline"


@dataclass
class ModelPickerState(SelectionState):
    options: dict[str, ModelOption] = dataclasses.field(default_factory=dict)


@dataclass
class DocumentBrowserState:
    source: str
    query: str = ""
    list_page: int = 0
    document_id: Optional[str] = None
    chunk_index: int = 0
    requester_user_id: Optional[str] = None
