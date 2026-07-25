"""Ownership boundary for per-workspace OpenCode supervisor processes.

`OpenCodeSupervisorPool` owns the resource-lifetime concerns that used to
live directly on `DiscordBotService`: a keyed cache of live
`OpenCodeSupervisor` instances (keyed by `"global"` or by workspace root
depending on repo config), the lock guarding that cache, TTL-based idle
pruning, and the background prune loop that drives it.

Collaborators are supplied explicitly through the constructor rather than a
back-reference to the owning service, so this module has no knowledge of
`DiscordBotService` itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path

DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS = 300.0
DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS = 600.0


def opencode_prune_interval(idle_ttl_seconds: Optional[int]) -> Optional[float]:
    if not idle_ttl_seconds or idle_ttl_seconds <= 0:
        return None
    return float(min(600.0, max(60.0, idle_ttl_seconds / 2)))


@dataclass
class OpenCodeSupervisorCacheEntry:
    supervisor: OpenCodeSupervisor
    prune_interval_seconds: Optional[float]
    last_requested_at: float


class OpenCodeSupervisorPool:
    """Owns the keyed cache, lock, and idle-prune lifecycle for OpenCode
    supervisors.

    Constructor dependencies are narrow, single-purpose callables rather
    than a reference to the owning `DiscordBotService`:

    - `logger`: where lifecycle/prune events are logged.
    - `hub_config_path`: resolved hub config path, used to load per-workspace
      repo config (fixed for the lifetime of the service).
    - `load_repo_config`: loads repo config for a workspace root.
    - `build_supervisor`: constructs an `OpenCodeSupervisor` from repo config.
    - `workspace_has_running_execution`: reports whether a workspace has an
      active OpenCode execution in flight, so the pruner can defer eviction.
      This is orchestration state that this pool does not own.
    - `publish_global_supervisor`: publishes the "global"-scoped supervisor
      to wherever the rest of the system looks it up (runtime services).
    - `register_owned_supervisor`: registers a supervisor as owned so its
      lifecycle is included in broader shutdown/close sweeps.
    - `log_event`: emits structured lifecycle/prune events. Defaults to the
      shared `log_event` helper; overridable so callers can route events
      through their own module (this is how tests observe these events by
      monkeypatching `log_event` on the caller's module).
    """

    def __init__(
        self,
        *,
        logger: logging.Logger,
        hub_config_path: Optional[Path],
        load_repo_config: Callable[..., Any],
        build_supervisor: Callable[..., Optional[OpenCodeSupervisor]],
        workspace_has_running_execution: Callable[[Path], Optional[bool]],
        publish_global_supervisor: Callable[[OpenCodeSupervisor], None],
        register_owned_supervisor: Callable[[OpenCodeSupervisor], None],
        log_event: Callable[..., None] = log_event,
    ) -> None:
        self._logger = logger
        self._hub_config_path = hub_config_path
        self._load_repo_config = load_repo_config
        self._build_supervisor = build_supervisor
        self._workspace_has_running_execution = workspace_has_running_execution
        self._publish_global_supervisor = publish_global_supervisor
        self._register_owned_supervisor = register_owned_supervisor
        self._log_event = log_event
        self.supervisors: dict[str, OpenCodeSupervisorCacheEntry] = {}
        self.lock = asyncio.Lock()

    async def supervisor_for_workspace(
        self, workspace_root: Path
    ) -> Optional[OpenCodeSupervisor]:
        repo_config = self._load_repo_config(
            workspace_root,
            hub_path=self._hub_config_path,
        )
        opencode_config = getattr(repo_config, "opencode", None)
        server_scope = getattr(opencode_config, "server_scope", "global")
        key = "global" if server_scope == "global" else str(workspace_root)
        async with self.lock:
            existing = self.supervisors.get(key)
            if existing is not None:
                existing.last_requested_at = time.monotonic()
                return existing.supervisor
            supervisor = self._build_supervisor(
                repo_config,
                workspace_root=workspace_root,
                logger=self._logger,
                base_env=None,
            )
            if supervisor is None:
                return None
            prune_ttl = getattr(opencode_config, "idle_ttl_seconds", None)
            self.supervisors[key] = OpenCodeSupervisorCacheEntry(
                supervisor=supervisor,
                prune_interval_seconds=opencode_prune_interval(prune_ttl),
                last_requested_at=time.monotonic(),
            )
            if key == "global":
                self._publish_global_supervisor(supervisor)
            self._register_owned_supervisor(supervisor)
            return supervisor

    async def next_prune_interval_seconds(self) -> float:
        async with self.lock:
            intervals = [
                entry.prune_interval_seconds
                for entry in self.supervisors.values()
                if entry.prune_interval_seconds is not None
            ]
            has_supervisors = bool(self.supervisors)
        if intervals:
            return cast(float, min(intervals))
        if not has_supervisors:
            return DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS
        return DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS

    async def run_prune_loop(self) -> None:
        while True:
            await asyncio.sleep(await self.next_prune_interval_seconds())
            await self.prune()

    async def prune(self) -> None:
        async with self.lock:
            cached_entries = list(self.supervisors.items())
        cached_supervisors = len(cached_entries)
        if not cached_entries:
            self._log_event(
                self._logger,
                logging.DEBUG,
                "discord.opencode.prune_sweep",
                cached_supervisors=0,
                cached_supervisors_after=0,
                live_handles=0,
                killed_processes=0,
                evicted_supervisors=0,
            )
            return

        now = time.monotonic()
        live_handles = 0
        killed_processes = 0
        eviction_candidates: list[tuple[str, OpenCodeSupervisorCacheEntry]] = []

        for workspace_path, entry in cached_entries:
            workspace_root = canonicalize_path(Path(workspace_path))
            execution_running = self._workspace_has_running_execution(workspace_root)
            if execution_running is not False:
                entry.last_requested_at = now
                try:
                    snapshot = await entry.supervisor.lifecycle_snapshot()
                except (OSError, RuntimeError, ValueError) as exc:
                    self._log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.opencode.prune_failed",
                        workspace_path=workspace_path,
                        exc=exc,
                    )
                else:
                    live_handles += snapshot.cached_handles
                self._log_event(
                    self._logger,
                    logging.DEBUG,
                    "discord.opencode.prune_deferred",
                    workspace_path=workspace_path,
                    reason=(
                        "active_runtime_execution"
                        if execution_running
                        else "execution_state_unknown"
                    ),
                )
                continue
            try:
                killed_processes += await entry.supervisor.prune_idle()
                snapshot = await entry.supervisor.lifecycle_snapshot()
            except (OSError, RuntimeError, ValueError) as exc:
                self._log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.opencode.prune_failed",
                    workspace_path=workspace_path,
                    exc=exc,
                )
                continue
            live_handles += snapshot.cached_handles
            idle_for = max(0.0, now - entry.last_requested_at)
            eviction_delay = (
                entry.prune_interval_seconds
                or DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS
            )
            if snapshot.cached_handles == 0 and idle_for >= eviction_delay:
                eviction_candidates.append((workspace_path, entry))

        evicted_supervisors = 0
        evicted_objects: list[OpenCodeSupervisor] = []
        if eviction_candidates:
            async with self.lock:
                for workspace_path, entry in eviction_candidates:
                    current = self.supervisors.get(workspace_path)
                    if current is not entry:
                        continue
                    self.supervisors.pop(workspace_path, None)
                    evicted_supervisors += 1
                    evicted_objects.append(entry.supervisor)
        for supervisor in evicted_objects:
            with contextlib.suppress(Exception):
                await supervisor.close_all()

        async with self.lock:
            cached_supervisors_after = len(self.supervisors)
        self._log_event(
            self._logger,
            logging.DEBUG,
            "discord.opencode.prune_sweep",
            cached_supervisors=cached_supervisors,
            cached_supervisors_after=cached_supervisors_after,
            live_handles=live_handles,
            killed_processes=killed_processes,
            evicted_supervisors=evicted_supervisors,
        )

    async def close_all(self) -> None:
        async with self.lock:
            supervisors = [entry.supervisor for entry in self.supervisors.values()]
            self.supervisors.clear()
        for supervisor in supervisors:
            with contextlib.suppress(Exception):
                await supervisor.close_all()

    async def clear_cache(self) -> None:
        """Drop cached entries without closing the underlying supervisors.

        Used when a broader runtime-services container has already closed
        the supervisor processes (via `register_owned_supervisor`) and this
        pool's cache just needs to be reset to match.
        """
        async with self.lock:
            self.supervisors.clear()
