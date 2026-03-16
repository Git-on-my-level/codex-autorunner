"""Core runtime primitives."""

from .archive import ArchiveResult, archive_worktree_snapshot
from .car_context import (
    DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE,
    DEFAULT_PMA_CONTEXT_PROFILE,
    DEFAULT_REPO_THREAD_CONTEXT_PROFILE,
    DEFAULT_TICKET_FLOW_CONTEXT_PROFILE,
    CarContextBundle,
    CarContextProfile,
    build_car_context_bundle,
    default_managed_thread_context_profile,
    render_injected_car_context,
    render_runtime_compat_agents_md,
)
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
    "CarContextBundle",
    "CarContextProfile",
    "CAR_AWARENESS_BLOCK",
    "DEFAULT_AGENT_WORKSPACE_CONTEXT_PROFILE",
    "DEFAULT_PMA_CONTEXT_PROFILE",
    "DEFAULT_REPO_THREAD_CONTEXT_PROFILE",
    "DEFAULT_TICKET_FLOW_CONTEXT_PROFILE",
    "build_car_context_bundle",
    "default_managed_thread_context_profile",
    "format_file_role_addendum",
    "LifecycleEvent",
    "LifecycleEventEmitter",
    "LifecycleEventStore",
    "LifecycleEventType",
    "PmaAutomationStore",
    "PmaAutomationTimer",
    "PmaAutomationWakeup",
    "PmaLifecycleSubscription",
    "render_injected_car_context",
    "render_runtime_compat_agents_md",
    "SSEEvent",
    "format_sse",
    "parse_sse_lines",
    "build_type_debt_ledger",
    "render_markdown_report",
    "ledger_to_dict",
]
