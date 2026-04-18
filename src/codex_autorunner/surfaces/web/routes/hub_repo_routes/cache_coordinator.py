from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .....core.logging_utils import safe_log

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .services import HubRepoEnricher


class HubCacheCoordinator:
    """Single owner of hub-wide cache invalidation after mutations.

    After any hub mutation (run, stop, cleanup, archive, destination change,
    worktree creation/removal), call ``await invalidate_caches()`` to ensure
    all projection and in-memory caches are reset consistently.

    The coordinator orders invalidation as:

    1. **Supervisor topology refresh** – forces the supervisor to re-scan
       repo/worktree state so subsequent listing snapshots see current data.
    2. **Repo-enricher runtime caches** – clears per-repo ticket-flow / run-state
       in-memory caches *and* the ``repo_runtime_v1`` projection-store namespace.
    3. **Hub message snapshot caches** – clears the hub-snapshot in-memory cache
       and ``hub_snapshot_v1`` / ``repo_capability_hints_v1`` projection-store
       namespaces so the message inbox and PMA action queue are refreshed.

    Each step is best-effort; failures are logged rather than propagated.
    """

    def __init__(
        self,
        context: HubAppContext,
        enricher: HubRepoEnricher,
    ) -> None:
        self._context = context
        self._enricher = enricher

    async def invalidate_caches(self) -> None:
        await self._refresh_topology()
        self._invalidate_repo_runtime_caches()
        self._invalidate_hub_snapshot_caches()

    async def _refresh_topology(self) -> None:
        try:
            await asyncio.to_thread(
                self._context.supervisor.list_repos, use_cache=False
            )
        except Exception as exc:
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub topology cache refresh failed",
                exc=exc,
            )

    def _invalidate_repo_runtime_caches(self) -> None:
        self._enricher.invalidate_runtime_caches()

    def _invalidate_hub_snapshot_caches(self) -> None:
        from ...services.hub_gather import invalidate_hub_message_snapshot_cache

        invalidate_hub_message_snapshot_cache(
            self._context,
            include_repo_capability_hints=True,
        )
