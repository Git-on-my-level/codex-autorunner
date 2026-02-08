"""Core runtime primitives."""

from .archive import ArchiveResult, archive_worktree_snapshot
from .context_awareness import CAR_AWARENESS_BLOCK, format_file_role_addendum
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)
from .sse import SSEEvent, format_sse, parse_sse_lines

__all__ = [
    "ArchiveResult",
    "archive_worktree_snapshot",
    "CAR_AWARENESS_BLOCK",
    "format_file_role_addendum",
    "LifecycleEvent",
    "LifecycleEventEmitter",
    "LifecycleEventStore",
    "LifecycleEventType",
    "SSEEvent",
    "format_sse",
    "parse_sse_lines",
]
