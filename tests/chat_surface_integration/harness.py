from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from codex_autorunner.agents.hermes.harness import HERMES_CAPABILITIES, HermesHarness
from codex_autorunner.agents.hermes.supervisor import HermesSupervisor
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService

DEFAULT_DISCORD_CHANNEL_ID = "channel-1"
DEFAULT_DISCORD_GUILD_ID = "guild-1"
DEFAULT_TELEGRAM_CHAT_ID = 123
DEFAULT_TELEGRAM_THREAD_ID = 55
DEFAULT_TELEGRAM_USER_ID = 456
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FAKE_ACP_FIXTURE_PATH = FIXTURES_DIR / "fake_acp_server.py"
APP_SERVER_FIXTURE_PATH = FIXTURES_DIR / "app_server_fixture.py"


def hermes_fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FAKE_ACP_FIXTURE_PATH), "--scenario", scenario]


def app_server_fixture_command(scenario: str = "basic") -> list[str]:
    return [
        sys.executable,
        "-u",
        str(APP_SERVER_FIXTURE_PATH),
        "--scenario",
        scenario,
    ]


@dataclass
class HermesFixtureRuntime:
    scenario: str
    logger_name: str = "test.chat_surface_integration.hermes"
    _descriptor: Optional[AgentDescriptor] = field(default=None, init=False)
    _supervisor: Optional[HermesSupervisor] = field(default=None, init=False)

    @property
    def supervisor(self) -> HermesSupervisor:
        if self._supervisor is None:
            self._supervisor = HermesSupervisor(
                hermes_fixture_command(self.scenario),
                logger=logging.getLogger(self.logger_name),
            )
        return self._supervisor

    def descriptor(self) -> AgentDescriptor:
        if self._descriptor is None:
            self._descriptor = AgentDescriptor(
                id="hermes",
                name="Hermes",
                capabilities=HERMES_CAPABILITIES,
                runtime_kind="hermes",
                make_harness=lambda _ctx: HermesHarness(self.supervisor),
            )
        return self._descriptor

    def registered_agents(self) -> dict[str, AgentDescriptor]:
        return {"hermes": self.descriptor()}

    async def close(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.close_all()
            self._supervisor = None


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


class FakeDiscordRest:
    def __init__(self) -> None:
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.message_ops: list[dict[str, Any]] = []

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        message = {"id": f"msg-{len(self.channel_messages) + 1}"}
        self.channel_messages.append(
            {"channel_id": channel_id, "payload": dict(payload)}
        )
        self.message_ops.append(
            {
                "op": "send",
                "channel_id": channel_id,
                "message_id": message["id"],
                "payload": dict(payload),
            }
        )
        return message

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": dict(payload),
            }
        )
        self.message_ops.append(
            {
                "op": "edit",
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": dict(payload),
            }
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )
        self.message_ops.append(
            {
                "op": "delete",
                "channel_id": channel_id,
                "message_id": message_id,
            }
        )

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        _ = application_id, guild_id
        return commands


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
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": text,
        "author": {"id": "user-1", "bot": False},
        "attachments": [],
    }


@dataclass
class DiscordSurfaceHarness:
    root: Path
    logger_name: str = "test.chat_surface_integration.discord"
    timeout_seconds: float = 2.0
    store: Optional[DiscordStateStore] = field(default=None, init=False)
    rest: Optional[FakeDiscordRest] = field(default=None, init=False)

    async def setup(self, *, agent: str = "hermes") -> None:
        seed_hub_files(self.root, force=True)
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
            pma_enabled=True,
        )
        await self.store.update_agent_state(
            channel_id=DEFAULT_DISCORD_CHANNEL_ID,
            agent=agent,
        )

    async def run_message(self, text: str) -> FakeDiscordRest:
        if self.store is None:
            raise RuntimeError("DiscordSurfaceHarness.setup() must run first")
        self.rest = FakeDiscordRest()
        service = DiscordBotService(
            make_discord_config(self.root),
            logger=logging.getLogger(self.logger_name),
            rest_client=self.rest,
            gateway_client=FakeDiscordGateway(
                [("MESSAGE_CREATE", build_discord_message_create(text))]
            ),
            state_store=self.store,
            outbox_manager=FakeDiscordOutboxManager(),
        )
        await asyncio.wait_for(service.run_forever(), timeout=self.timeout_seconds)
        return self.rest

    async def close(self) -> None:
        if self.store is not None:
            await self.store.close()
            self.store = None


class FakeTelegramBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.deleted_messages: list[dict[str, Any]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        _ = parse_mode, disable_web_page_preview
        self.messages.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": len(self.messages)}

    async def send_message_chunks(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        max_len: int = 4096,
    ) -> list[dict[str, Any]]:
        _ = parse_mode, disable_web_page_preview, max_len
        self.messages.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return [{"message_id": len(self.messages)}]

    async def send_document(
        self,
        chat_id: int,
        document: bytes,
        *,
        filename: str,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        _ = document, parse_mode
        self.documents.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "filename": filename,
                "caption": caption,
            }
        )
        return {"message_id": len(self.documents)}

    async def answer_callback_query(
        self,
        _callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        _ = text, show_alert
        return {}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        _ = parse_mode, disable_web_page_preview
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": message_id}

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        self.deleted_messages.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "message_id": message_id,
            }
        )
        return True


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
    thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
        is_topic_message=True,
        chat_type="supergroup",
    )


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

    async def setup(
        self,
        *,
        agent: str = "hermes",
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
    ) -> None:
        self.service = TelegramBotService(
            make_telegram_config(self.root),
            hub_root=self.root,
        )
        self.service._logger = logging.getLogger(self.logger_name)
        self.bot = FakeTelegramBot()
        self.service._bot = self.bot
        await self.service._router.ensure_topic(DEFAULT_TELEGRAM_CHAT_ID, thread_id)
        await self.service._router.update_topic(
            DEFAULT_TELEGRAM_CHAT_ID,
            thread_id,
            lambda record: _configure_telegram_pma_topic(record, agent=agent),
        )

    async def run_message(
        self,
        text: str,
        *,
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
    ) -> FakeTelegramBot:
        if self.service is None or self.bot is None:
            raise RuntimeError("TelegramSurfaceHarness.setup() must run first")
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
        return self.bot

    async def close(self) -> None:
        if self.service is not None:
            await self.service._app_server_supervisor.close_all()
            self.service = None
        self.bot = None


def _configure_telegram_pma_topic(record: Any, *, agent: str) -> None:
    record.pma_enabled = True
    record.workspace_path = None
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
