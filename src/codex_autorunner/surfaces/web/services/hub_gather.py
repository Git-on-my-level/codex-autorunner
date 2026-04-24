from __future__ import annotations

from typing import Any, Optional

from ....core import hub_read_model as _core_hub_read_model
from ....core.hub_read_model import (
    HUB_SNAPSHOT_PROJECTION_NAMESPACE,
    REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
    _collect_pma_files_detail,
    _collect_pma_threads,
    _gather_inbox,
    _hub_snapshot_cache,
    _HubSnapshotCacheEntry,
    _repo_capability_hint_cache,
    _RepoCapabilityHintCacheEntry,
    _snapshot_pma_automation,
    build_hub_capability_hints,
    build_repo_capability_hints,
    get_hub_read_model_service,
    invalidate_hub_message_snapshot_cache,
    latest_dispatch,
    load_hub_inbox_dismissals,
)


def _sync_core_dependencies() -> None:
    _core_hub_read_model._gather_inbox = _gather_inbox
    _core_hub_read_model._collect_pma_files_detail = _collect_pma_files_detail
    _core_hub_read_model._collect_pma_threads = _collect_pma_threads
    _core_hub_read_model._snapshot_pma_automation = _snapshot_pma_automation
    _core_hub_read_model.build_hub_capability_hints = build_hub_capability_hints
    _core_hub_read_model.build_repo_capability_hints = build_repo_capability_hints
    _core_hub_read_model.load_hub_inbox_dismissals = load_hub_inbox_dismissals


def gather_hub_message_snapshot(
    context: Any,
    *,
    limit: int = 100,
    scope_key: Optional[str] = None,
    sections: Optional[set[str]] = None,
) -> dict[str, Any]:
    _sync_core_dependencies()
    return get_hub_read_model_service(context).gather_message_snapshot(
        limit=limit,
        scope_key=scope_key,
        sections=sections,
    )


__all__ = [
    "HUB_SNAPSHOT_PROJECTION_NAMESPACE",
    "REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE",
    "_HubSnapshotCacheEntry",
    "_RepoCapabilityHintCacheEntry",
    "_collect_pma_files_detail",
    "_collect_pma_threads",
    "_gather_inbox",
    "_hub_snapshot_cache",
    "_repo_capability_hint_cache",
    "_snapshot_pma_automation",
    "build_hub_capability_hints",
    "build_repo_capability_hints",
    "gather_hub_message_snapshot",
    "invalidate_hub_message_snapshot_cache",
    "latest_dispatch",
    "load_hub_inbox_dismissals",
]
