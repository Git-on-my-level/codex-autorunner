from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from fastapi import FastAPI
from starlette.routing import Mount
from starlette.types import ASGIApp

from .....core.config import ConfigError
from .....core.logging_utils import safe_log

if TYPE_CHECKING:
    from ...app_state import HubAppContext


class HubMountManager:
    def __init__(
        self,
        app: FastAPI,
        context: HubAppContext,
        build_repo_app: Callable[[Path], ASGIApp],
    ) -> None:
        self.app = app
        self.context = context
        self._build_repo_app = build_repo_app

        self._mounted_repos: set[str] = set()
        self._mount_errors: dict[str, str] = {}
        self._repo_apps: dict[str, ASGIApp] = {}
        self._repo_lifespans: dict[str, AbstractAsyncContextManager[Any]] = {}
        self._mount_order: list[str] = []
        self._mount_lock: Optional[asyncio.Lock] = None

    async def _get_mount_lock(self) -> asyncio.Lock:
        if self._mount_lock is None:
            self._mount_lock = asyncio.Lock()
        return self._mount_lock

    @staticmethod
    def _unwrap_fastapi(sub_app: ASGIApp) -> Optional[FastAPI]:
        current: ASGIApp = sub_app
        while not isinstance(current, FastAPI):
            nested = getattr(current, "app", None)
            if nested is None:
                return None
            current = nested
        return current

    async def _start_repo_lifespan_locked(self, prefix: str, sub_app: ASGIApp) -> None:
        if prefix in self._repo_lifespans:
            return
        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is None:
            return
        try:
            lifespan_context = cast(
                Callable[[FastAPI], AbstractAsyncContextManager[Any]],
                fastapi_app.router.lifespan_context,
            )
            ctx = lifespan_context(fastapi_app)
            await ctx.__aenter__()
            self._repo_lifespans[prefix] = ctx
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan entered for {prefix}",
            )
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning(
                    "Repo lifespan failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan failure for {prefix}",
                    exc=exc2,
                )
            await self._unmount_repo_locked(prefix)

    async def _stop_repo_lifespan_locked(self, prefix: str) -> None:
        ctx = self._repo_lifespans.pop(prefix, None)
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan exited for {prefix}",
            )
        except Exception as exc:
            try:
                self.app.state.logger.warning(
                    "Repo lifespan shutdown failed for %s: %s", prefix, exc
                )
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan shutdown failure for {prefix}",
                    exc=exc2,
                )

    def _detach_mount_locked(self, prefix: str) -> None:
        mount_path = f"/repos/{prefix}"
        self.app.router.routes = [
            route
            for route in self.app.router.routes
            if not (isinstance(route, Mount) and route.path == mount_path)
        ]
        self._mounted_repos.discard(prefix)
        self._repo_apps.pop(prefix, None)
        if prefix in self._mount_order:
            self._mount_order.remove(prefix)

    async def _unmount_repo_locked(self, prefix: str) -> None:
        await self._stop_repo_lifespan_locked(prefix)
        self._detach_mount_locked(prefix)

    def _mount_repo_sync(self, prefix: str, repo_path: Path) -> bool:
        if prefix in self._mounted_repos:
            return True
        if prefix in self._mount_errors:
            return False
        try:
            sub_app = self._build_repo_app(repo_path)
        except ConfigError as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        except Exception as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except Exception as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False

        fastapi_app = self._unwrap_fastapi(sub_app)
        if fastapi_app is not None:
            fastapi_app.state.repo_id = prefix

        self.app.mount(f"/repos/{prefix}", sub_app)
        self._mounted_repos.add(prefix)
        self._repo_apps[prefix] = sub_app
        if prefix not in self._mount_order:
            self._mount_order.append(prefix)
        self._mount_errors.pop(prefix, None)
        return True

    def mount_initial(self, snapshots: Iterable[Any]) -> None:
        for snapshot in snapshots:
            if getattr(snapshot, "initialized", False) and getattr(
                snapshot, "exists_on_disk", False
            ):
                self._mount_repo_sync(snapshot.id, snapshot.path)

    async def refresh_mounts(
        self, snapshots: Iterable[Any], *, full_refresh: bool = True
    ):
        desired = {
            snapshot.id
            for snapshot in snapshots
            if getattr(snapshot, "initialized", False)
            and getattr(snapshot, "exists_on_disk", False)
        }
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            if full_refresh:
                for prefix in list(self._mounted_repos):
                    if prefix not in desired:
                        await self._unmount_repo_locked(prefix)
                for prefix in list(self._mount_errors):
                    if prefix not in desired:
                        self._mount_errors.pop(prefix, None)

            for snapshot in snapshots:
                if snapshot.id not in desired:
                    continue
                if (
                    snapshot.id in self._mounted_repos
                    or snapshot.id in self._mount_errors
                ):
                    continue
                if not self._mount_repo_sync(snapshot.id, snapshot.path):
                    continue
                fastapi_app = self._unwrap_fastapi(self._repo_apps[snapshot.id])
                if fastapi_app is not None:
                    fastapi_app.state.repo_id = snapshot.id
                if self.app.state.hub_started:
                    await self._start_repo_lifespan_locked(
                        snapshot.id, self._repo_apps[snapshot.id]
                    )

    async def start_repo_lifespans(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(self._mount_order):
                sub_app = self._repo_apps.get(prefix)
                if sub_app is not None:
                    await self._start_repo_lifespan_locked(prefix, sub_app)

    async def stop_repo_mounts(self) -> None:
        mount_lock = await self._get_mount_lock()
        async with mount_lock:
            for prefix in list(reversed(self._mount_order)):
                await self._stop_repo_lifespan_locked(prefix)
            for prefix in list(self._mounted_repos):
                self._detach_mount_locked(prefix)

    def add_mount_info(self, repo_dict: dict) -> dict:
        repo_id = repo_dict.get("id")
        if repo_id in self._mount_errors:
            repo_dict["mounted"] = False
            repo_dict["mount_error"] = self._mount_errors[repo_id]
        elif repo_id in self._mounted_repos:
            repo_dict["mounted"] = True
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        else:
            repo_dict["mounted"] = False
            if "mount_error" in repo_dict:
                repo_dict.pop("mount_error", None)
        return repo_dict
