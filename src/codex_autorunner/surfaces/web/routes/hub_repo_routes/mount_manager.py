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

_LEGACY_FRONTEND_TABS = {
    "workspace",
    "tickets",
    "messages",
    "inbox",
    "contextspace",
    "archive",
    "analytics",
    "terminal",
    "settings",
}


def _is_legacy_frontend_opt_in(query_string: bytes) -> bool:
    return any(
        part in {b"legacy=1", b"legacy=true", b"debug=1", b"debug=true"}
        for part in query_string.split(b"&")
    )


async def _send_redirect(send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", location.encode("utf-8", errors="replace"))],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _send_legacy_debug_page(send, *, location: str, legacy_location: str) -> None:
    body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Legacy CAR UI</title>
  </head>
  <body>
    <main>
      <h1>Legacy/debug route</h1>
      <p>This old CAR UI route is retained for migration and debugging only.</p>
      <p><a href="{location}">Open the PMA Hub route</a></p>
      <p><a href="{legacy_location}">Open this legacy screen</a></p>
    </main>
  </body>
</html>
"""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        }
    )
    await send(
        {"type": "http.response.body", "body": body.encode("utf-8", errors="replace")}
    )


class _LazyRepoApp:
    """Build and start a repo sub-app on first use instead of at hub startup."""

    def __init__(
        self,
        *,
        prefix: str,
        repo_path: Path,
        build_repo_app: Callable[[Path], ASGIApp],
        logger: logging.Logger,
        hub_started: Callable[[], bool],
    ) -> None:
        self.prefix = prefix
        self.repo_path = repo_path
        self._build_repo_app = build_repo_app
        self._logger = logger
        self._hub_started = hub_started
        self._build_lock: Optional[asyncio.Lock] = None
        self._sub_app: Optional[ASGIApp] = None
        self._lifespan: Optional[AbstractAsyncContextManager[Any]] = None
        self._build_error: Optional[str] = None

    @property
    def build_error(self) -> Optional[str]:
        return self._build_error

    @property
    def app(self) -> Optional[ASGIApp]:
        return self._sub_app

    async def _ensure_ready(self) -> ASGIApp:
        if self._sub_app is not None:
            return self._sub_app
        if self._build_lock is None:
            self._build_lock = asyncio.Lock()
        async with self._build_lock:
            if self._sub_app is not None:
                return self._sub_app
            try:
                sub_app = await asyncio.to_thread(self._build_repo_app, self.repo_path)
                fastapi_app = HubMountManager._unwrap_fastapi(sub_app)
                if fastapi_app is not None:
                    fastapi_app.state.repo_id = self.prefix
                    if self._hub_started():
                        lifespan_context = cast(
                            Callable[[FastAPI], AbstractAsyncContextManager[Any]],
                            fastapi_app.router.lifespan_context,
                        )
                        self._lifespan = lifespan_context(fastapi_app)
                        await self._lifespan.__aenter__()
                self._sub_app = sub_app
                self._build_error = None
            except Exception as exc:
                self._build_error = str(exc)
                safe_log(
                    self._logger,
                    logging.WARNING,
                    "Lazy repo mount failed for %s",
                    self.prefix,
                    exc=exc,
                )
                raise
        return self._sub_app

    async def close(self) -> None:
        ctx = self._lifespan
        self._lifespan = None
        if ctx is None:
            return
        try:
            await ctx.__aexit__(None, None, None)
        except Exception as exc:
            safe_log(
                self._logger,
                logging.WARNING,
                "Repo lifespan shutdown failed for %s",
                self.prefix,
                exc=exc,
            )

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path") or "")
            query_string = scope.get("query_string") or b""
            root_path = str(scope.get("root_path") or f"/repos/{self.prefix}")
            pma_repo_location = root_path.rstrip("/") or f"/repos/{self.prefix}"
            tab = path.strip("/").split("/", 1)[0]
            legacy_mount = root_path.rstrip("/").endswith(
                f"/legacy/repos/{self.prefix}"
            )
            if (
                not legacy_mount
                and path in {"", "/"}
                and not _is_legacy_frontend_opt_in(query_string)
            ):
                await _send_redirect(send, pma_repo_location)
                return
            if (
                not legacy_mount
                and tab in _LEGACY_FRONTEND_TABS
                and not _is_legacy_frontend_opt_in(query_string)
            ):
                query_text = query_string.decode("utf-8", errors="replace")
                legacy_location = (
                    f"{root_path}{path}?{query_text}&legacy=1"
                    if query_text
                    else f"{root_path}{path}?legacy=1"
                )
                await _send_legacy_debug_page(
                    send,
                    location=pma_repo_location,
                    legacy_location=legacy_location,
                )
                return
        try:
            sub_app = await self._ensure_ready()
        except Exception:
            message = self._build_error or "Repo mount failed"
            if scope.get("type") == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1011,
                        "reason": message[:120],
                    }
                )
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": message.encode("utf-8", errors="replace"),
                }
            )
            return
        await sub_app(scope, receive, send)


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
        except (
            Exception
        ) as exc:  # intentional: arbitrary user-provided lifespan context
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning(
                    "Repo lifespan failed for %s: %s", prefix, exc
                )
            except (RuntimeError, OSError) as exc2:
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
            sub_app = self._repo_apps.get(prefix)
            close_fn = getattr(sub_app, "close", None)
            if close_fn is None or not callable(close_fn):
                return
            await close_fn()
            return
        try:
            await ctx.__aexit__(None, None, None)
            safe_log(
                self.app.state.logger,
                logging.INFO,
                f"Repo app lifespan exited for {prefix}",
            )
        except (
            RuntimeError,
            OSError,
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            try:
                self.app.state.logger.warning(
                    "Repo lifespan shutdown failed for %s: %s", prefix, exc
                )
            except (RuntimeError, OSError) as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log repo lifespan shutdown failure for {prefix}",
                    exc=exc2,
                )

    def _mount_paths(self, prefix: str) -> tuple[str, str]:
        return (f"/repos/{prefix}", f"/legacy/repos/{prefix}")

    def _mount_path(self, prefix: str) -> str:
        return f"/repos/{prefix}"

    def _detach_mount_locked(self, prefix: str) -> None:
        mount_paths = set(self._mount_paths(prefix))
        self.app.router.routes = [
            route
            for route in self.app.router.routes
            if not (isinstance(route, Mount) and route.path in mount_paths)
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
            sub_app = _LazyRepoApp(
                prefix=prefix,
                repo_path=repo_path,
                build_repo_app=self._build_repo_app,
                logger=self.app.state.logger,
                hub_started=lambda: bool(getattr(self.app.state, "hub_started", False)),
            )
        except ConfigError as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except (RuntimeError, OSError) as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            ImportError,
        ) as exc:
            self._mount_errors[prefix] = str(exc)
            try:
                self.app.state.logger.warning("Cannot mount repo %s: %s", prefix, exc)
            except (RuntimeError, OSError) as exc2:
                safe_log(
                    self.app.state.logger,
                    logging.DEBUG,
                    f"Failed to log mount error for {prefix}",
                    exc=exc2,
                )
            return False

        self.app.mount(self._mount_path(prefix), sub_app)
        self.app.mount(f"/legacy/repos/{prefix}", sub_app)
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
        if not isinstance(repo_id, str) or not repo_id:
            repo_dict["mounted"] = False
            repo_dict.pop("mount_error", None)
            return repo_dict
        sub_app = self._repo_apps.get(repo_id)
        lazy_error = getattr(sub_app, "build_error", None)
        if isinstance(lazy_error, str) and lazy_error:
            self._mount_errors[repo_id] = lazy_error
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
