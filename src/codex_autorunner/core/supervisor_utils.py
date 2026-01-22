from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

from .logging_utils import log_event

HandleT = TypeVar("HandleT", bound=Any)


def evict_lru_handle_locked(
    handles: dict[str, HandleT],
    max_handles: int | None,
    logger: logging.Logger,
    event_prefix: str,
    *,
    last_used_at_getter: Callable[[HandleT], float],
) -> HandleT | None:
    if not max_handles or max_handles <= 0:
        return None
    if len(handles) < max_handles:
        return None
    lru_handle = min(handles.values(), key=last_used_at_getter)
    log_event(
        logger,
        logging.INFO,
        f"{event_prefix}.handle.evicted",
        reason="max_handles",
        workspace_id=lru_handle.workspace_id,
        workspace_root=str(lru_handle.workspace_root),
        max_handles=max_handles,
        handle_count=len(handles),
        last_used_at=last_used_at_getter(lru_handle),
    )
    handles.pop(lru_handle.workspace_id, None)
    return lru_handle


def pop_idle_handles_locked(
    handles: dict[str, HandleT],
    idle_ttl_seconds: float | None,
    logger: logging.Logger,
    event_prefix: str,
    *,
    last_used_at_getter: Callable[[HandleT], float],
    should_skip_prune: Callable[[HandleT], bool] | None = None,
) -> list[HandleT]:
    if not idle_ttl_seconds or idle_ttl_seconds <= 0:
        return []
    cutoff = time.monotonic() - idle_ttl_seconds
    stale: list[HandleT] = []
    for handle in list(handles.values()):
        if should_skip_prune and should_skip_prune(handle):
            log_event(
                logger,
                logging.INFO,
                f"{event_prefix}.handle.prune.skipped",
                reason="should_skip",
                workspace_id=handle.workspace_id,
                workspace_root=str(handle.workspace_root),
            )
            continue
        if last_used_at_getter(handle) and last_used_at_getter(handle) < cutoff:
            handles.pop(handle.workspace_id, None)
            stale.append(handle)
    return stale
