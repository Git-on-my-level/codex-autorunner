from __future__ import annotations

from typing import Any, Literal, Optional

ManagedThreadChatKind = Literal["pma", "coding_agent"]

MANAGED_THREAD_CHAT_KIND_PMA: ManagedThreadChatKind = "pma"
MANAGED_THREAD_CHAT_KIND_CODING_AGENT: ManagedThreadChatKind = "coding_agent"
MANAGED_THREAD_CHAT_KIND_VALUES: tuple[ManagedThreadChatKind, ...] = (
    MANAGED_THREAD_CHAT_KIND_PMA,
    MANAGED_THREAD_CHAT_KIND_CODING_AGENT,
)

_CODING_AGENT_ALIASES = {
    "agent",
    "coding-agent",
    "coding_agent",
    "direct-agent",
    "direct_agent",
}


def normalize_managed_thread_chat_kind(
    value: Any,
    *,
    default: Optional[ManagedThreadChatKind] = MANAGED_THREAD_CHAT_KIND_PMA,
) -> Optional[ManagedThreadChatKind]:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized == MANAGED_THREAD_CHAT_KIND_PMA:
        return MANAGED_THREAD_CHAT_KIND_PMA
    if normalized in _CODING_AGENT_ALIASES:
        return MANAGED_THREAD_CHAT_KIND_CODING_AGENT
    return default


def infer_managed_thread_chat_kind(
    *,
    metadata: Any = None,
    display_name: Any = None,
) -> ManagedThreadChatKind:
    metadata_map = metadata if isinstance(metadata, dict) else {}
    explicit = normalize_managed_thread_chat_kind(
        metadata_map.get("chat_kind") or metadata_map.get("thread_kind"),
        default=None,
    )
    if explicit is not None:
        return explicit
    if isinstance(display_name, str) and "coding agent" in display_name.strip().lower():
        return MANAGED_THREAD_CHAT_KIND_CODING_AGENT
    return MANAGED_THREAD_CHAT_KIND_PMA


__all__ = [
    "MANAGED_THREAD_CHAT_KIND_CODING_AGENT",
    "MANAGED_THREAD_CHAT_KIND_PMA",
    "MANAGED_THREAD_CHAT_KIND_VALUES",
    "ManagedThreadChatKind",
    "infer_managed_thread_chat_kind",
    "normalize_managed_thread_chat_kind",
]
