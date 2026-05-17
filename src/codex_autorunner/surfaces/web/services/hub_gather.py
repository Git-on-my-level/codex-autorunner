from __future__ import annotations

from typing import Any, Optional

from ....core import hub_read_model
from ....core.hub_read_model import (
    HUB_SNAPSHOT_PROJECTION_NAMESPACE,
    REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE,
    HubMessageSnapshotCollectors,
    build_hub_capability_hints,
    build_repo_capability_hints,
    get_hub_read_model_service,
    invalidate_hub_message_snapshot_cache,
    latest_dispatch,
)

_HubSnapshotCacheEntry = hub_read_model._HubSnapshotCacheEntry
_RepoCapabilityHintCacheEntry = hub_read_model._RepoCapabilityHintCacheEntry
_collect_managed_threads = hub_read_model._collect_managed_threads
_collect_pma_files_detail = hub_read_model._collect_pma_files_detail
_gather_inbox = hub_read_model._gather_inbox
_hub_snapshot_cache = hub_read_model._hub_snapshot_cache
_repo_capability_hint_cache = hub_read_model._repo_capability_hint_cache
_snapshot_pma_automation = hub_read_model._snapshot_pma_automation
load_hub_inbox_dismissals = hub_read_model.load_hub_inbox_dismissals


def default_message_snapshot_collectors() -> HubMessageSnapshotCollectors:
    return HubMessageSnapshotCollectors(
        gather_inbox=_gather_inbox,
        collect_pma_files_detail=_collect_pma_files_detail,
        collect_managed_threads=_collect_managed_threads,
        snapshot_pma_automation=_snapshot_pma_automation,
        build_hub_capability_hints=build_hub_capability_hints,
        build_repo_capability_hints=build_repo_capability_hints,
        load_hub_inbox_dismissals=load_hub_inbox_dismissals,
    )


def gather_hub_message_snapshot(
    context: Any,
    *,
    limit: int = 100,
    scope_key: Optional[str] = None,
    sections: Optional[set[str]] = None,
    collectors: Optional[HubMessageSnapshotCollectors] = None,
) -> dict[str, Any]:
    return get_hub_read_model_service(
        context,
        message_snapshot_collectors=collectors or default_message_snapshot_collectors(),
    ).gather_message_snapshot(
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
    "_collect_managed_threads",
    "_gather_inbox",
    "_hub_snapshot_cache",
    "_repo_capability_hint_cache",
    "_snapshot_pma_automation",
    "build_hub_capability_hints",
    "build_repo_capability_hints",
    "default_message_snapshot_collectors",
    "gather_hub_message_snapshot",
    "invalidate_hub_message_snapshot_cache",
    "latest_dispatch",
    "load_hub_inbox_dismissals",
]
