"""Core runtime primitives."""

from .archive import ArchiveResult, archive_worktree_snapshot
from .context_awareness import CAR_AWARENESS_BLOCK, format_file_role_addendum
from .lifecycle_events import (
    LifecycleEvent,
    LifecycleEventEmitter,
    LifecycleEventStore,
    LifecycleEventType,
)
from .pma_automation_store import (
    PmaAutomationStore,
    PmaAutomationTimer,
    PmaAutomationWakeup,
    PmaLifecycleSubscription,
)
from .sse import SSEEvent, format_sse, parse_sse_lines
from .type_debt_ledger import (
    build_type_debt_ledger,
    ledger_to_dict,
    render_markdown_report,
)

__all__ = [
    "ArchiveResult",
    "archive_worktree_snapshot",
    "CAR_AWARENESS_BLOCK",
    "format_file_role_addendum",
    "LifecycleEvent",
    "LifecycleEventEmitter",
    "LifecycleEventStore",
    "LifecycleEventType",
    "PmaAutomationStore",
    "PmaAutomationTimer",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "SSEEvent",
    "format_sse",
    "parse_sse_lines",
    "build_type_debt_ledger",
    "render_markdown_report",
    "ledger_to_dict",
]
