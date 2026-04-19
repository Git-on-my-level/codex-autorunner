from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TranscriptParty(str, Enum):
    """Logical actor recorded in a normalized transcript."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    PLATFORM = "platform"


class TranscriptEventKind(str, Enum):
    """Surface-visible operations that later lab runners will normalize."""

    ACK = "ack"
    SEND = "send"
    EDIT = "edit"
    DELETE = "delete"
    STATUS = "status"
    CALLBACK = "callback"
    ATTACHMENT = "attachment"
    ERROR = "error"


@dataclass(frozen=True)
class TranscriptEvent:
    """One normalized event emitted by a chat surface during a scenario run."""

    kind: TranscriptEventKind
    party: TranscriptParty
    timestamp_ms: int
    surface_kind: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptTimeline:
    """Ordered transcript plus scenario metadata for rendering and diffing."""

    scenario_id: str
    events: tuple[TranscriptEvent, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
