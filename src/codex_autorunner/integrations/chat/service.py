"""Chat service core orchestration for adapter-driven bot runtimes.

This module lives in the adapter layer (`integrations/chat`) and provides a
platform-agnostic core with pluggable adapters/dispatchers, plus a compatibility
orchestrator used while Telegram behavior is being extracted incrementally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.managed_processes import reap_managed_processes
from .adapter import ChatAdapter
from .dispatcher import ChatDispatcher
from .models import ChatEvent
from .state_store import ChatStateStore
from .transport import ChatTransport


class ChatBotServiceCore:
    """Core orchestration layer for chat surfaces.

    The core can run:
    - adapter-native event loops (`run_adapter_loop`) with `ChatDispatcher`, and
    - transitional Telegram polling orchestration (`run`) while extraction is in flight.
    """

    def __init__(
        self,
        *,
        owner: Any,
        runtime_services: Any,
        state_store: ChatStateStore,
        adapter: Optional[ChatAdapter] = None,
        transport: Optional[ChatTransport] = None,
        dispatcher: Optional[ChatDispatcher] = None,
        platform: str = "telegram",
    ) -> None:
        self._owner = owner
        self._runtime_services = runtime_services
        self._state_store = state_store
        self._adapter = adapter
        self._transport = transport
        self._dispatcher = dispatcher or ChatDispatcher(logger=owner._logger)
        self._platform = platform

    async def run(self) -> None:
        """Run transitional orchestration for Telegram polling mode."""

        owner = self._owner
        if owner._config.mode != "polling":
            raise RuntimeError(f"Unsupported telegram_bot.mode '{owner._config.mode}'")
        owner._config.validate()
        try:
            cleanup = reap_managed_processes(owner._config.root)
            if cleanup.killed or cleanup.signaled or cleanup.removed:
                log_event(
                    owner._logger,
                    logging.INFO,
                    f"{self._platform}.process_reaper.cleaned",
                    killed=cleanup.killed,
                    signaled=cleanup.signaled,
                    removed=cleanup.removed,
                    skipped=cleanup.skipped,
                )
        except Exception as exc:
            log_event(
                owner._logger,
                logging.WARNING,
                f"{self._platform}.process_reaper.failed",
                exc=exc,
            )
        owner._acquire_instance_lock()
        owner._turn_semaphore = asyncio.Semaphore(
            owner._config.concurrency.max_parallel_turns
        )
        owner._outbox_manager.start()
        owner._voice_manager.start()
        try:
            await owner._prime_bot_identity()
            await owner._register_bot_commands()
            await owner._restore_pending_approvals()
            await owner._outbox_manager.restore()
            await owner._voice_manager.restore()
            await owner._prime_poller_offset()
            owner._outbox_task = asyncio.create_task(owner._outbox_manager.run_loop())
            owner._voice_task = asyncio.create_task(owner._voice_manager.run_loop())
            owner._housekeeping_task = asyncio.create_task(owner._housekeeping_loop())
            owner._cache_cleanup_task = asyncio.create_task(owner._cache_cleanup_loop())
            owner._ticket_flow_watch_task = asyncio.create_task(
                owner._ticket_flow_bridge.watch_ticket_flow_pauses(
                    owner.TICKET_FLOW_WATCH_INTERVAL_SECONDS
                )
            )
            owner._spawn_task(owner._prewarm_workspace_clients())
            log_event(
                owner._logger,
                logging.INFO,
                f"{self._platform}.bot.started",
                mode=owner._config.mode,
                poll_timeout=owner._config.poll_timeout_seconds,
                allowed_updates=list(owner._config.poll_allowed_updates),
                allowed_chats=len(owner._config.allowed_chat_ids),
                allowed_users=len(owner._config.allowed_user_ids),
                require_topics=owner._config.require_topics,
                max_parallel_turns=owner._config.concurrency.max_parallel_turns,
                per_topic_queue=owner._config.concurrency.per_topic_queue,
                media_enabled=owner._config.media.enabled,
                media_images=owner._config.media.images,
                media_voice=owner._config.media.voice,
                app_server_turn_timeout_seconds=owner._config.app_server_turn_timeout_seconds,
                agent_turn_timeout_seconds=dict(
                    owner._config.agent_turn_timeout_seconds
                ),
                poller_offset=owner._poller.offset,
            )
            try:
                await owner._maybe_send_update_status_notice()
            except Exception as exc:
                log_event(
                    owner._logger,
                    logging.WARNING,
                    f"{self._platform}.update.notify_failed",
                    exc=exc,
                )
            try:
                await owner._maybe_send_compact_status_notice()
            except Exception as exc:
                log_event(
                    owner._logger,
                    logging.WARNING,
                    f"{self._platform}.compact.notify_failed",
                    exc=exc,
                )
            while True:
                updates = []
                try:
                    updates = await owner._poller.poll(
                        timeout=owner._config.poll_timeout_seconds
                    )
                    if owner._poller.offset is not None:
                        await owner._record_poll_offset(updates)
                except Exception as exc:
                    log_event(
                        owner._logger,
                        logging.WARNING,
                        f"{self._platform}.poll.failed",
                        exc=exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                for update in updates:
                    owner._spawn_task(owner._dispatch_update(update))
        finally:
            try:
                if owner._outbox_task is not None:
                    owner._outbox_task.cancel()
                    try:
                        await owner._outbox_task
                    except asyncio.CancelledError:
                        pass
                if owner._voice_task is not None:
                    owner._voice_task.cancel()
                    try:
                        await owner._voice_task
                    except asyncio.CancelledError:
                        pass
                if owner._housekeeping_task is not None:
                    owner._housekeeping_task.cancel()
                    try:
                        await owner._housekeeping_task
                    except asyncio.CancelledError:
                        pass
                if owner._cache_cleanup_task is not None:
                    owner._cache_cleanup_task.cancel()
                    try:
                        await owner._cache_cleanup_task
                    except asyncio.CancelledError:
                        pass
                if owner._ticket_flow_watch_task is not None:
                    owner._ticket_flow_watch_task.cancel()
                    try:
                        await owner._ticket_flow_watch_task
                    except asyncio.CancelledError:
                        pass
                if owner._spawned_tasks:
                    for task in list(owner._spawned_tasks):
                        task.cancel()
                    await asyncio.gather(*owner._spawned_tasks, return_exceptions=True)
            finally:
                try:
                    await owner._bot.close()
                except Exception as exc:
                    log_event(
                        owner._logger,
                        logging.WARNING,
                        f"{self._platform}.bot.close_failed",
                        exc=exc,
                    )
                try:
                    await self._runtime_services.close()
                except Exception as exc:
                    log_event(
                        owner._logger,
                        logging.WARNING,
                        f"{self._platform}.runtime_services.close_failed",
                        exc=exc,
                    )
                try:
                    await self._state_store.close()
                except Exception as exc:
                    log_event(
                        owner._logger,
                        logging.WARNING,
                        f"{self._platform}.state.close_failed",
                        exc=exc,
                    )
                owner._release_instance_lock()

    async def run_adapter_loop(
        self,
        *,
        handle_event: Callable[[ChatEvent], Awaitable[None]],
        poll_timeout_seconds: float = 30.0,
    ) -> None:
        """Run an adapter-native event loop and dispatch via `ChatDispatcher`."""

        if self._adapter is None:
            raise RuntimeError("Chat adapter is not configured")
        while True:
            events = await self._adapter.poll_events(
                timeout_seconds=poll_timeout_seconds
            )
            for event in events:
                await self._dispatcher.dispatch(
                    event,
                    lambda evt, _ctx: handle_event(evt),
                )
