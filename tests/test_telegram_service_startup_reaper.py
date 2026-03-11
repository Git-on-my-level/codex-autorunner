from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.chat import service as chat_service_module
from codex_autorunner.integrations.chat.service import ChatBotServiceCore


class _NeverEndingManager:
    def start(self) -> None:
        return None

    async def restore(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


class _TicketFlowBridgeStub:
    async def watch_ticket_flow_pauses(self, _interval: float) -> None:
        await asyncio.Event().wait()


class _BotStub:
    async def close(self) -> None:
        return None


class _RuntimeServicesStub:
    async def close(self) -> None:
        return None


class _StateStoreStub:
    async def close(self) -> None:
        return None


class _PollerStub:
    def __init__(self) -> None:
        self.offset = None

    async def poll(self, *, timeout: int) -> list[object]:
        raise KeyboardInterrupt("stop after startup")


class _OwnerStub:
    TICKET_FLOW_WATCH_INTERVAL_SECONDS = 1

    def __init__(self, root: Path) -> None:
        self._logger = logging.getLogger("test.telegram.startup")
        self._config = SimpleNamespace(
            mode="polling",
            root=root,
            validate=lambda: None,
            concurrency=SimpleNamespace(max_parallel_turns=1, per_topic_queue=True),
            poll_timeout_seconds=1,
            poll_allowed_updates=["message"],
            allowed_chat_ids={123},
            allowed_user_ids={456},
            require_topics=False,
            media=SimpleNamespace(enabled=False, images=False, voice=False),
            app_server_turn_timeout_seconds=None,
            agent_turn_timeout_seconds={},
        )
        self._outbox_manager = _NeverEndingManager()
        self._voice_manager = _NeverEndingManager()
        self._ticket_flow_bridge = _TicketFlowBridgeStub()
        self._bot = _BotStub()
        self._turn_semaphore = None
        self._outbox_task = None
        self._voice_task = None
        self._housekeeping_task = None
        self._cache_cleanup_task = None
        self._ticket_flow_watch_task = None
        self._spawned_tasks: set[asyncio.Task[object]] = set()
        self._poller = _PollerStub()

    def _acquire_instance_lock(self) -> None:
        return None

    def _release_instance_lock(self) -> None:
        return None

    async def _prime_bot_identity(self) -> None:
        return None

    async def _register_bot_commands(self) -> None:
        return None

    async def _restore_pending_approvals(self) -> None:
        return None

    async def _prime_poller_offset(self) -> None:
        return None

    async def _housekeeping_loop(self) -> None:
        await asyncio.Event().wait()

    async def _cache_cleanup_loop(self) -> None:
        await asyncio.Event().wait()

    async def _prewarm_workspace_clients(self) -> None:
        return None

    async def _maybe_send_update_status_notice(self) -> None:
        return None

    async def _maybe_send_compact_status_notice(self) -> None:
        return None

    async def _record_poll_offset(self, _updates: list[object]) -> None:
        return None

    async def _dispatch_update(self, _update: object) -> None:
        return None

    def _spawn_task(self, coro):
        task = asyncio.create_task(coro)
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)
        return task


@pytest.mark.anyio
async def test_telegram_service_startup_reaps_managed_processes(
    tmp_path: Path, monkeypatch
) -> None:
    called_roots: list[Path] = []

    def _fake_reap(root: Path) -> SimpleNamespace:
        called_roots.append(root)
        return SimpleNamespace(killed=0, signaled=0, removed=0, skipped=0)

    monkeypatch.setattr(chat_service_module, "reap_managed_processes", _fake_reap)

    owner = _OwnerStub(tmp_path)
    core = ChatBotServiceCore(
        owner=owner,
        runtime_services=_RuntimeServicesStub(),
        state_store=_StateStoreStub(),
    )

    with pytest.raises(KeyboardInterrupt, match="stop after startup"):
        await core.run()

    assert called_roots == [tmp_path]
