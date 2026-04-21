from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.integrations.chat.models import (
    ChatAttachment,
    ChatMessageEvent,
    ChatMessageRef,
    ChatThreadRef,
)
from codex_autorunner.integrations.discord.components import (
    build_cancel_turn_custom_id,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.message_turns import (
    build_discord_thread_orchestration_service,
)
from codex_autorunner.integrations.discord.outbox import DiscordOutboxManager
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramMessage,
)
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    _build_telegram_thread_orchestration_service,
)
from codex_autorunner.integrations.telegram.service import TelegramBotService
from tests.chat_surface_lab.backend_runtime import (
    HermesFixtureRuntime,
    app_server_fixture_command,
    fake_acp_command,
)
from tests.chat_surface_lab.discord_simulator import (
    DiscordSimulatorFaults,
    DiscordSurfaceSimulator,
)
from tests.chat_surface_lab.telegram_simulator import (
    TelegramSimulatorFaults,
    TelegramSurfaceSimulator,
)
from tests.conftest import write_test_config

DEFAULT_DISCORD_CHANNEL_ID = "channel-1"
DEFAULT_DISCORD_GUILD_ID = "guild-1"
DEFAULT_TELEGRAM_CHAT_ID = 123
DEFAULT_TELEGRAM_THREAD_ID = 55
DEFAULT_TELEGRAM_USER_ID = 456
hermes_fixture_command = fake_acp_command


class StructuredLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            payload = {"message": message}
        if not isinstance(payload, dict):
            payload = {"message": str(payload)}
        self.records.append(payload)


def patch_hermes_runtime(monkeypatch: Any, runtime: HermesFixtureRuntime) -> None:
    def _registered(_context: Any = None) -> dict[str, AgentDescriptor]:
        return runtime.registered_agents()

    monkeypatch.setattr(
        "codex_autorunner.agents.registry.get_registered_agents",
        _registered,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.message_turns.get_registered_agents",
        _registered,
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.commands.execution.get_registered_agents",
        _registered,
    )


class FakeDiscordRest(DiscordSurfaceSimulator):
    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[str]] = None,
        faults: Optional[DiscordSimulatorFaults] = None,
        attachment_data_by_url: Optional[dict[str, bytes]] = None,
    ) -> None:
        super().__init__(
            fail_delete_message_ids=fail_delete_message_ids,
            faults=faults,
            attachment_data_by_url=attachment_data_by_url,
        )


class FakeDiscordGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = list(events)

    async def run(self, on_dispatch: Any) -> None:
        for event_type, payload in self._events:
            await on_dispatch(event_type, payload)
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        return None


class FakeDiscordOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


def make_discord_config(root: Path) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({DEFAULT_DISCORD_GUILD_ID}),
        allowed_channel_ids=frozenset({DEFAULT_DISCORD_CHANNEL_ID}),
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=False,
            scope="guild",
            guild_ids=(DEFAULT_DISCORD_GUILD_ID,),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=True,
        shell=DiscordBotShellConfig(
            enabled=True,
            timeout_ms=120000,
            max_output_chars=3800,
        ),
        media=DiscordBotMediaConfig(
            enabled=True,
            voice=True,
            max_voice_bytes=10 * 1024 * 1024,
        ),
        collaboration_policy=None,
    )


def build_discord_message_create(
    text: str,
    *,
    message_id: str = "m-1",
    guild_id: str = DEFAULT_DISCORD_GUILD_ID,
    channel_id: str = DEFAULT_DISCORD_CHANNEL_ID,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": text,
        "author": {"id": "user-1", "bot": False},
        "attachments": list(attachments or []),
    }


@dataclass
class DiscordSurfaceHarness:
    root: Path
    logger_name: str = "test.chat_surface_integration.discord"
    timeout_seconds: float = 2.0
    store: Optional[DiscordStateStore] = field(default=None, init=False)
    rest: Optional[FakeDiscordRest] = field(default=None, init=False)
    service: Optional[DiscordBotService] = field(default=None, init=False)
    _log_capture: Optional[StructuredLogCapture] = field(default=None, init=False)

    async def setup(
        self,
        *,
        agent: str = "hermes",
        approval_mode: Optional[str] = None,
        pma_enabled: bool = True,
    ) -> None:
        seed_hub_files(self.root, force=True)
        config_payload = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
        config_payload["discord_bot"]["state_file"] = "discord_state.sqlite3"
        config_payload["telegram_bot"]["state_file"] = "telegram_state.sqlite3"
        write_test_config(self.root / CONFIG_FILENAME, config_payload)
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        self.store = DiscordStateStore(self.root / "discord_state.sqlite3")
        await self.store.initialize()
        await self.store.upsert_binding(
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            guild_id=DEFAULT_DISCORD_GUILD_ID,
            workspace_path=str(workspace),
            repo_id="repo-1",
        )
        await self.store.update_pma_state(
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            pma_enabled=pma_enabled,
        )
        await self.store.update_agent_state(
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            agent=agent,
        )
        if isinstance(approval_mode, str) and approval_mode.strip():
            await self.store.update_approval_mode(
                channel_id=DEFAULT_DISCORD_CHANNEL_ID,
                mode=approval_mode,
            )

    def orchestration_service(self) -> Any:
        if self.service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        return build_discord_thread_orchestration_service(self.service)

    async def wait_for_log_event(
        self,
        event_name: str,
        *,
        timeout_seconds: float = 2.0,
        predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
    ) -> dict[str, Any]:
        if self._log_capture is None:
            raise RuntimeError("DiscordSurfaceHarness has no active log capture")
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        while True:
            for record in self._log_capture.records:
                if record.get("event") != event_name:
                    continue
                if predicate is not None and not predicate(record):
                    continue
                return record
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"DiscordSurfaceHarness missing log event: {event_name}"
                )
            await asyncio.sleep(0.01)

    async def submit_active_message(
        self,
        text: str,
        *,
        message_id: str = "m-2",
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        if self.service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        thread = ChatThreadRef(
            platform="discord",
            chat_id=DEFAULT_DISCORD_CHANNEL_ID,
            thread_id=DEFAULT_DISCORD_GUILD_ID,
        )
        event = ChatMessageEvent(
            update_id=message_id,
            thread=thread,
            message=ChatMessageRef(thread=thread, message_id=message_id),
            from_user_id="user-1",
            text=text,
            attachments=tuple(
                ChatAttachment(
                    kind=str(item.get("kind") or "document"),
                    file_id=str(item.get("id") or f"att-{index}"),
                    file_name=(
                        str(item.get("filename"))
                        if item.get("filename") is not None
                        else None
                    ),
                    mime_type=(
                        str(item.get("content_type"))
                        if item.get("content_type") is not None
                        else None
                    ),
                    size_bytes=(
                        int(item.get("size")) if item.get("size") is not None else None
                    ),
                    source_url=(
                        str(item.get("url")) if item.get("url") is not None else None
                    ),
                )
                for index, item in enumerate(attachments or [], start=1)
            ),
        )
        self.service._command_runner.submit_event(event)

    def start_active_message(
        self,
        text: str,
        *,
        message_id: str = "m-2",
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self.submit_active_message(
                text,
                message_id=message_id,
                attachments=attachments,
            )
        )

    async def _run_gateway_events_inner(
        self,
        events: list[tuple[str, dict[str, Any]]],
        *,
        rest_client: Optional[FakeDiscordRest] = None,
    ) -> FakeDiscordRest:
        if self.store is None:
            raise RuntimeError("DiscordSurfaceHarness.setup() must run first")
        self.rest = rest_client or FakeDiscordRest()
        logger = logging.getLogger(self.logger_name)
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.propagate = True
        self._log_capture = StructuredLogCapture()
        logger.addHandler(self._log_capture)
        self.service = DiscordBotService(
            make_discord_config(self.root),
            logger=logger,
            rest_client=self.rest,
            gateway_client=FakeDiscordGateway(events),
            state_store=self.store,
            outbox_manager=FakeDiscordOutboxManager(),
        )
        await asyncio.wait_for(
            self.service.run_forever(),
            timeout=self.timeout_seconds,
        )
        self._apply_discord_runtime_metadata(self.rest)
        return self.rest

    async def _run_message_inner(
        self,
        text: str,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> FakeDiscordRest:
        return await self._run_gateway_events_inner(
            [
                (
                    "MESSAGE_CREATE",
                    build_discord_message_create(
                        text,
                        attachments=attachments,
                    ),
                )
            ],
            rest_client=rest_client,
        )

    def start_message(
        self,
        text: str,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> asyncio.Task[FakeDiscordRest]:
        return asyncio.create_task(
            self._run_message_inner(
                text,
                rest_client=rest_client,
                attachments=attachments,
            )
        )

    async def run_message(
        self,
        text: str,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> FakeDiscordRest:
        return await self._run_message_inner(
            text,
            rest_client=rest_client,
            attachments=attachments,
        )

    async def run_gateway_events(
        self,
        events: list[tuple[str, dict[str, Any]]],
        *,
        rest_client: Optional[FakeDiscordRest] = None,
    ) -> FakeDiscordRest:
        return await self._run_gateway_events_inner(events, rest_client=rest_client)

    def _apply_discord_runtime_metadata(self, rest: FakeDiscordRest) -> None:
        service = self.service
        if service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        rest.log_records = list(self._log_capture.records)
        rest.background_tasks_drained = not bool(service._background_tasks)
        rest.surface_key = DEFAULT_DISCORD_CHANNEL_ID
        orchestration_service = build_discord_thread_orchestration_service(service)
        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key=DEFAULT_DISCORD_CHANNEL_ID,
        )
        thread_target_id = (
            str(getattr(binding, "thread_target_id", "") or "").strip() or None
        )
        rest.thread_target_id = thread_target_id
        if thread_target_id:
            execution = orchestration_service.get_latest_execution(thread_target_id)
            if execution is not None:
                rest.execution_id = (
                    str(getattr(execution, "execution_id", "") or "").strip() or None
                )
                rest.execution_status = (
                    str(getattr(execution, "status", "") or "").strip() or None
                )
                rest.execution_error = (
                    str(getattr(execution, "error", "") or "").strip() or None
                )
        if rest.message_ops:
            preview_message_id = str(rest.message_ops[0].get("message_id") or "")
            rest.preview_message_id = preview_message_id or None
            rest.preview_deleted = any(
                op["op"] == "delete"
                and str(op.get("message_id") or "") == preview_message_id
                for op in rest.message_ops
            )
            terminal_edit = next(
                (
                    op
                    for op in reversed(rest.message_ops)
                    if op["op"] == "edit"
                    and str(op.get("message_id") or "") == preview_message_id
                ),
                None,
            )
            if terminal_edit is not None:
                content = str(terminal_edit["payload"].get("content", "")).lower()
                if "done" in content:
                    rest.terminal_progress_label = "done"
                elif "failed" in content:
                    rest.terminal_progress_label = "failed"
                elif "working" in content:
                    rest.terminal_progress_label = "working"

    async def drain_outbox(
        self,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
    ) -> FakeDiscordRest:
        if self.store is None:
            raise RuntimeError("DiscordSurfaceHarness.setup() must run first")
        rest = rest_client or self.rest or FakeDiscordRest()
        fresh_store = DiscordStateStore(self.store.path)
        await fresh_store.initialize()
        try:
            manager = DiscordOutboxManager(
                fresh_store,
                send_message=lambda channel_id, payload: rest.create_channel_message(
                    channel_id=channel_id,
                    payload=payload,
                ),
                delete_message=lambda channel_id, message_id: rest.delete_channel_message(
                    channel_id=channel_id,
                    message_id=message_id,
                ),
                logger=logging.getLogger(self.logger_name),
                retry_interval_seconds=0.01,
                immediate_retry_delays=(0.0,),
            )
            manager.start()
            records = await fresh_store.list_outbox()
            if records:
                await manager._flush(records)
        finally:
            await fresh_store.close()
        self.rest = rest
        self._apply_discord_runtime_metadata(rest)
        return rest

    async def wait_for_running_execution(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        while True:
            service = self.service
            if service is not None:
                orchestration_service = build_discord_thread_orchestration_service(
                    service
                )
                binding = orchestration_service.get_binding(
                    surface_kind="discord",
                    surface_key=DEFAULT_DISCORD_CHANNEL_ID,
                )
                thread_target_id = (
                    str(getattr(binding, "thread_target_id", "") or "").strip() or None
                )
                if thread_target_id:
                    execution = orchestration_service.get_latest_execution(
                        thread_target_id
                    )
                    if execution is not None:
                        status = str(getattr(execution, "status", "") or "").strip()
                        execution_id = str(
                            getattr(execution, "execution_id", "") or ""
                        ).strip()
                        if status == "running" and execution_id:
                            return thread_target_id, execution_id
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "DiscordSurfaceHarness did not expose a running turn"
                )
            await asyncio.sleep(0.01)

    async def wait_for_execution_status(
        self,
        expected_status: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        while True:
            service = self.service
            if service is not None:
                orchestration_service = build_discord_thread_orchestration_service(
                    service
                )
                binding = orchestration_service.get_binding(
                    surface_kind="discord",
                    surface_key=DEFAULT_DISCORD_CHANNEL_ID,
                )
                thread_target_id = (
                    str(getattr(binding, "thread_target_id", "") or "").strip() or None
                )
                if thread_target_id:
                    execution = orchestration_service.get_latest_execution(
                        thread_target_id
                    )
                    if execution is not None:
                        status = str(getattr(execution, "status", "") or "").strip()
                        execution_id = str(
                            getattr(execution, "execution_id", "") or ""
                        ).strip()
                        if status == expected_status and execution_id:
                            return thread_target_id, execution_id
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "DiscordSurfaceHarness did not expose the expected turn status"
                )
            await asyncio.sleep(0.01)

    async def interrupt_active_turn_via_component(
        self,
        *,
        thread_target_id: str,
        execution_id: str,
        interaction_id: str = "interaction-1",
        interaction_token: str = "token-1",
        user_id: str = "user-1",
    ) -> None:
        if self.service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        if self.rest is None:
            raise RuntimeError("DiscordSurfaceHarness has no active preview message")
        preview_message_id = self.rest.preview_message_id
        if preview_message_id is None:
            first_send = next(
                (op for op in self.rest.message_ops if op["op"] == "send"),
                None,
            )
            preview_message_id = (
                str(first_send.get("message_id") or "").strip() if first_send else ""
            ) or None
        if preview_message_id is None:
            raise RuntimeError("DiscordSurfaceHarness has no active preview message")
        await self.service._handle_cancel_turn_button(
            interaction_id,
            interaction_token,
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            user_id=user_id,
            message_id=preview_message_id,
            custom_id=build_cancel_turn_custom_id(
                thread_target_id=thread_target_id,
                execution_id=execution_id,
            ),
        )

    async def queue_interrupt_send_via_component(
        self,
        *,
        source_message_id: str = "m-2",
        interaction_id: str = "queue-interrupt-1",
        interaction_token: str = "queue-interrupt-token-1",
        user_id: str = "user-1",
        message_id: Optional[str] = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        if self.service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        if self.rest is None:
            raise RuntimeError("DiscordSurfaceHarness has no active rest client")
        if message_id is None:
            target_custom_id = f"queue_interrupt_send:{source_message_id}"
            deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
            while message_id is None:
                for op in reversed(self.rest.message_ops):
                    payload = op.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    components = payload.get("components")
                    if not isinstance(components, list):
                        continue
                    found = False
                    for row in components:
                        row_components = (
                            row.get("components") if isinstance(row, dict) else None
                        )
                        if not isinstance(row_components, list):
                            continue
                        for button in row_components:
                            if (
                                isinstance(button, dict)
                                and str(button.get("custom_id") or "")
                                == target_custom_id
                            ):
                                found = True
                                break
                        if found:
                            break
                    if not found:
                        continue
                    candidate = op.get("message_id")
                    if isinstance(candidate, str) and candidate.strip():
                        message_id = candidate
                        break
                if message_id is not None:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        "DiscordSurfaceHarness could not find queue interrupt button"
                    )
                await asyncio.sleep(0.01)
        await self.service._handle_queue_interrupt_send_button(
            interaction_id,
            interaction_token,
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            custom_id=f"queue_interrupt_send:{source_message_id}",
            guild_id=DEFAULT_DISCORD_GUILD_ID,
            user_id=user_id,
            message_id=message_id,
        )

    async def close(self) -> None:
        if self.store is not None:
            await self.store.close()
            self.store = None
        self.service = None
        if self._log_capture is not None:
            logging.getLogger(self.logger_name).removeHandler(self._log_capture)
            self._log_capture = None


class FakeTelegramBot(TelegramSurfaceSimulator):
    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[int]] = None,
        faults: Optional[TelegramSimulatorFaults] = None,
    ) -> None:
        super().__init__(
            fail_delete_message_ids=fail_delete_message_ids,
            faults=faults,
        )


def make_telegram_config(root: Path) -> TelegramBotConfig:
    raw = {
        "enabled": True,
        "mode": "polling",
        "allowed_chat_ids": [DEFAULT_TELEGRAM_CHAT_ID],
        "allowed_user_ids": [DEFAULT_TELEGRAM_USER_ID],
        "require_topics": False,
        "app_server_command": app_server_fixture_command("basic"),
    }
    env = {
        "CAR_TELEGRAM_BOT_TOKEN": "test-token",
        "CAR_TELEGRAM_CHAT_ID": str(DEFAULT_TELEGRAM_CHAT_ID),
    }
    return TelegramBotConfig.from_raw(raw, root=root, env=env)


def build_telegram_message(
    text: str,
    *,
    thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
    message_id: int = 1,
    update_id: int = 1,
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=DEFAULT_TELEGRAM_CHAT_ID,
        thread_id=thread_id,
        from_user_id=DEFAULT_TELEGRAM_USER_ID,
        text=text,
        date=0,
        is_topic_message=thread_id is not None,
        chat_type="supergroup",
    )


def _telegram_surface_key(thread_id: Optional[int]) -> str:
    thread_segment = thread_id if thread_id is not None else "root"
    return f"{DEFAULT_TELEGRAM_CHAT_ID}:{thread_segment}"


async def drain_telegram_spawned_tasks(service: TelegramBotService) -> None:
    while True:
        while service._spawned_tasks:
            await asyncio.gather(*tuple(service._spawned_tasks))
        for runtime in service._router._topics.values():
            await runtime.queue.join_idle()
        if not service._spawned_tasks:
            return


@dataclass
class TelegramSurfaceHarness:
    root: Path
    logger_name: str = "test.chat_surface_integration.telegram"
    timeout_seconds: float = 2.0
    service: Optional[TelegramBotService] = field(default=None, init=False)
    bot: Optional[FakeTelegramBot] = field(default=None, init=False)
    _log_capture: Optional[StructuredLogCapture] = field(default=None, init=False)

    async def setup(
        self,
        *,
        agent: str = "hermes",
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        approval_mode: Optional[str] = None,
        pma_enabled: bool = True,
    ) -> None:
        seed_hub_files(self.root, force=True)
        config_payload = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
        config_payload["discord_bot"]["state_file"] = "discord_state.sqlite3"
        config_payload["telegram_bot"]["state_file"] = "telegram_state.sqlite3"
        write_test_config(self.root / CONFIG_FILENAME, config_payload)
        self.service = TelegramBotService(
            make_telegram_config(self.root),
            hub_root=self.root,
        )
        logger = logging.getLogger(self.logger_name)
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.propagate = True
        self._log_capture = StructuredLogCapture()
        logger.addHandler(self._log_capture)
        self.service._logger = logger
        self.bot = FakeTelegramBot()
        self.service._bot = self.bot
        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        await self.service._router.ensure_topic(DEFAULT_TELEGRAM_CHAT_ID, thread_id)
        await self.service._router.update_topic(
            DEFAULT_TELEGRAM_CHAT_ID,
            thread_id,
            lambda record: (
                _configure_telegram_pma_topic(record, agent=agent)
                if pma_enabled
                else _configure_telegram_repo_topic(
                    record,
                    agent=agent,
                    workspace_path=str(workspace),
                )
            ),
        )
        if isinstance(approval_mode, str) and approval_mode.strip():
            await self.service._router.set_approval_mode(
                DEFAULT_TELEGRAM_CHAT_ID,
                thread_id,
                approval_mode,
            )

    def orchestration_service(self) -> Any:
        if self.service is None:
            raise RuntimeError("TelegramSurfaceHarness has no active service")
        return _build_telegram_thread_orchestration_service(self.service)

    async def wait_for_log_event(
        self,
        event_name: str,
        *,
        timeout_seconds: float = 2.0,
        predicate: Optional[Callable[[dict[str, Any]], bool]] = None,
    ) -> dict[str, Any]:
        if self._log_capture is None:
            raise RuntimeError("TelegramSurfaceHarness has no active log capture")
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        while True:
            for record in self._log_capture.records:
                if record.get("event") != event_name:
                    continue
                if predicate is not None and not predicate(record):
                    continue
                return record
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"TelegramSurfaceHarness missing log event: {event_name}"
                )
            await asyncio.sleep(0.01)

    async def _run_message_inner(
        self,
        text: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        bot_client: Optional[FakeTelegramBot] = None,
    ) -> FakeTelegramBot:
        if self.service is None:
            raise RuntimeError("TelegramSurfaceHarness.setup() must run first")
        if bot_client is not None:
            self.bot = bot_client
            self.service._bot = bot_client
        if self.bot is None:
            raise RuntimeError("TelegramSurfaceHarness.setup() must run first")
        message_start_index = len(self.bot.messages)
        await asyncio.wait_for(
            self.service._handle_message_inner(
                build_telegram_message(text, thread_id=thread_id)
            ),
            timeout=self.timeout_seconds,
        )
        await asyncio.wait_for(
            drain_telegram_spawned_tasks(self.service),
            timeout=self.timeout_seconds,
        )
        self.bot.background_tasks_drained = True
        self._apply_telegram_runtime_metadata(
            self.bot,
            thread_id=thread_id,
            message_start_index=message_start_index,
        )
        return self.bot

    def start_message(
        self,
        text: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        bot_client: Optional[FakeTelegramBot] = None,
    ) -> asyncio.Task[FakeTelegramBot]:
        return asyncio.create_task(
            self._run_message_inner(
                text,
                thread_id=thread_id,
                bot_client=bot_client,
            )
        )

    async def run_message(
        self,
        text: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        bot_client: Optional[FakeTelegramBot] = None,
    ) -> FakeTelegramBot:
        return await self._run_message_inner(
            text,
            thread_id=thread_id,
            bot_client=bot_client,
        )

    async def submit_active_message(
        self,
        text: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        message_id: int = 2,
        update_id: int = 2,
    ) -> None:
        if self.service is None:
            raise RuntimeError("TelegramSurfaceHarness.setup() must run first")
        await self.service._handle_message_inner(
            build_telegram_message(
                text,
                thread_id=thread_id,
                message_id=message_id,
                update_id=update_id,
            )
        )

    def start_active_message(
        self,
        text: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        message_id: int = 2,
        update_id: int = 2,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self.submit_active_message(
                text,
                thread_id=thread_id,
                message_id=message_id,
                update_id=update_id,
            )
        )

    def _apply_telegram_runtime_metadata(
        self,
        bot: FakeTelegramBot,
        *,
        thread_id: Optional[int],
        message_start_index: int = 0,
    ) -> None:
        service = self.service
        if service is None:
            raise RuntimeError("TelegramSurfaceHarness has no active service")
        bot.log_records = list(self._log_capture.records)
        bot.surface_key = _telegram_surface_key(thread_id)
        orchestration_service = _build_telegram_thread_orchestration_service(service)
        binding = orchestration_service.get_binding(
            surface_kind="telegram",
            surface_key=bot.surface_key,
        )
        thread_target_id = (
            str(getattr(binding, "thread_target_id", "") or "").strip() or None
        )
        bot.thread_target_id = thread_target_id
        if thread_target_id:
            execution = orchestration_service.get_latest_execution(thread_target_id)
            if execution is not None:
                bot.execution_id = (
                    str(getattr(execution, "execution_id", "") or "").strip() or None
                )
                bot.execution_status = (
                    str(getattr(execution, "status", "") or "").strip() or None
                )
                bot.execution_error = (
                    str(getattr(execution, "error", "") or "").strip() or None
                )
        run_messages = bot.messages[message_start_index:]
        if run_messages:
            first_message_id = run_messages[0].get("message_id")
            bot.placeholder_message_id = (
                int(first_message_id)
                if isinstance(first_message_id, int)
                else bot.placeholder_message_id
            )
            bot.placeholder_deleted = any(
                item.get("message_id") == bot.placeholder_message_id
                for item in bot.deleted_messages
            )

    async def wait_for_running_execution(
        self,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        timeout_seconds: float = 2.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        surface_key = _telegram_surface_key(thread_id)
        while True:
            service = self.service
            if service is not None:
                orchestration_service = _build_telegram_thread_orchestration_service(
                    service
                )
                binding = orchestration_service.get_binding(
                    surface_kind="telegram",
                    surface_key=surface_key,
                )
                thread_target_id = (
                    str(getattr(binding, "thread_target_id", "") or "").strip() or None
                )
                if thread_target_id:
                    execution = orchestration_service.get_latest_execution(
                        thread_target_id
                    )
                    if execution is not None:
                        status = str(getattr(execution, "status", "") or "").strip()
                        execution_id = str(
                            getattr(execution, "execution_id", "") or ""
                        ).strip()
                        if status == "running" and execution_id:
                            return thread_target_id, execution_id
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "TelegramSurfaceHarness did not expose a running turn"
                )
            await asyncio.sleep(0.01)

    async def wait_for_execution_status(
        self,
        expected_status: str,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        timeout_seconds: float = 2.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        surface_key = _telegram_surface_key(thread_id)
        while True:
            service = self.service
            if service is not None:
                orchestration_service = _build_telegram_thread_orchestration_service(
                    service
                )
                binding = orchestration_service.get_binding(
                    surface_kind="telegram",
                    surface_key=surface_key,
                )
                thread_target_id = (
                    str(getattr(binding, "thread_target_id", "") or "").strip() or None
                )
                if thread_target_id:
                    execution = orchestration_service.get_latest_execution(
                        thread_target_id
                    )
                    if execution is not None:
                        status = str(getattr(execution, "status", "") or "").strip()
                        execution_id = str(
                            getattr(execution, "execution_id", "") or ""
                        ).strip()
                        if status == expected_status and execution_id:
                            return thread_target_id, execution_id
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "TelegramSurfaceHarness did not expose the expected turn status"
                )
            await asyncio.sleep(0.01)

    async def interrupt_active_turn_via_callback(
        self,
        *,
        thread_id: Optional[int] = DEFAULT_TELEGRAM_THREAD_ID,
        callback_id: str = "cb-1",
        message_id: int = 1,
    ) -> None:
        if self.service is None:
            raise RuntimeError("TelegramSurfaceHarness has no active service")
        callback = TelegramCallbackQuery(
            update_id=999,
            callback_id=callback_id,
            from_user_id=DEFAULT_TELEGRAM_USER_ID,
            data="cancel",
            message_id=message_id,
            chat_id=DEFAULT_TELEGRAM_CHAT_ID,
            thread_id=thread_id,
        )
        await self.service._handle_interrupt_callback(callback)

    async def close(self) -> None:
        if self.service is not None:
            await self.service._app_server_supervisor.close_all()
            self.service = None
        self.bot = None
        if self._log_capture is not None:
            logging.getLogger(self.logger_name).removeHandler(self._log_capture)
            self._log_capture = None


def _configure_telegram_pma_topic(record: Any, *, agent: str) -> None:
    record.pma_enabled = True
    record.workspace_path = None
    record.repo_id = "repo-1"
    record.agent = agent
    record.agent_profile = None


def _configure_telegram_repo_topic(
    record: Any, *, agent: str, workspace_path: str
) -> None:
    record.pma_enabled = False
    record.workspace_path = workspace_path
    record.repo_id = "repo-1"
    record.agent = agent
    record.agent_profile = None


__all__ = [
    "DiscordSurfaceHarness",
    "FakeDiscordRest",
    "FakeTelegramBot",
    "HermesFixtureRuntime",
    "TelegramSurfaceHarness",
    "app_server_fixture_command",
    "build_discord_message_create",
    "build_telegram_message",
    "drain_telegram_spawned_tasks",
    "hermes_fixture_command",
    "patch_hermes_runtime",
]
