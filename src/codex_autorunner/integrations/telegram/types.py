from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from typing import Optional

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
    message_id: Optional[int]
    created_at: str
    question_index: int
    prompt: str
    options: list[str]
    future: asyncio.Future[Optional[int]]


@dataclass
class TurnContext:
    topic_key: str
    chat_id: int
    thread_id: Optional[int]
    codex_thread_id: Optional[str]
    reply_to_message_id: Optional[int]
    placeholder_message_id: Optional[int] = None


@dataclass
class CompactState:
    summary_text: str
    display_text: str
    message_id: int
    created_at: str


@dataclass
class SelectionState:
    items: list[tuple[str, str]]
    page: int = 0
    button_labels: Optional[dict[str, str]] = None


@dataclass
class ReviewCommitSelectionState(SelectionState):
    delivery: str = "inline"


@dataclass
class ModelPickerState(SelectionState):
    options: dict[str, ModelOption] = dataclasses.field(default_factory=dict)
