from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException

from .....core.runtime import LockError

if TYPE_CHECKING:
    from ...app_state import HubAppContext
    from .mount_manager import HubMountManager
    from .services import HubRepoEnricher


class HubRunControlService:
    def __init__(
        self,
        context: HubAppContext,
        mount_manager: HubMountManager,
        enricher: HubRepoEnricher,
    ) -> None:
        self._context = context
        self._mount_manager = mount_manager
        self._enricher = enricher

    async def _refresh_repo_listing_cache(self) -> None:
        from .....core.logging_utils import safe_log

        try:
            await asyncio.to_thread(
                self._context.supervisor.list_repos, use_cache=False
            )
        except Exception as exc:  # intentional: cache refresh is best-effort
            safe_log(
                self._context.logger,
                logging.WARNING,
                "Hub repo listing refresh failed after runtime mutation",
                exc=exc,
            )

    async def run_repo(self, repo_id: str, once: bool = False) -> dict:
        from .....core.logging_utils import safe_log

        once_val = once
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub run %s once=%s" % (repo_id, once_val),
        )
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.run_repo, repo_id, once=once_val
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            Exception
        ) as exc:  # intentional: pluggable spawn_fn may raise unpredictable errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)

    async def stop_repo(self, repo_id: str) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(self._context.logger, logging.INFO, f"Hub stop {repo_id}")
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.stop_repo, repo_id
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)

    async def resume_repo(self, repo_id: str, once: bool = False) -> dict:
        from .....core.logging_utils import safe_log

        once_val = once
        safe_log(
            self._context.logger,
            logging.INFO,
            "Hub resume %s once=%s" % (repo_id, once_val),
        )
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.resume_repo, repo_id, once=once_val
            )
        except LockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (
            Exception
        ) as exc:  # intentional: pluggable spawn_fn may raise unpredictable errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)

    async def kill_repo(self, repo_id: str) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(self._context.logger, logging.INFO, f"Hub kill {repo_id}")
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.kill_repo, repo_id
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)

    async def init_repo(self, repo_id: str) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(self._context.logger, logging.INFO, f"Hub init {repo_id}")
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.init_repo, repo_id
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)

    async def sync_main(self, repo_id: str) -> dict:
        from .....core.logging_utils import safe_log

        safe_log(self._context.logger, logging.INFO, f"Hub sync main {repo_id}")
        try:
            snapshot = await asyncio.to_thread(
                self._context.supervisor.sync_main, repo_id
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._refresh_repo_listing_cache()
        await self._mount_manager.refresh_mounts([snapshot], full_refresh=False)
        self._enricher.invalidate_runtime_caches()
        return self._enricher.enrich_repo(snapshot)
