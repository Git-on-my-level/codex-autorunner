from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.integrations.chat.models import (
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


class FakeDiscordRest:
    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[str]] = None,
    ) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.message_ops: list[dict[str, Any]] = []
        self.fail_delete_message_ids = set(fail_delete_message_ids or set())
        self.log_records: list[dict[str, Any]] = []
        self.surface_key: Optional[str] = None
        self.thread_target_id: Optional[str] = None
        self.execution_id: Optional[str] = None
        self.execution_status: Optional[str] = None
        self.execution_error: Optional[str] = None
        self.preview_message_id: Optional[str] = None
        self.preview_deleted: bool = False
        self.terminal_progress_label: Optional[str] = None
        self.background_tasks_drained: bool = False

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": f"followup-{len(self.followup_messages)}"}

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": "@original"}

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

    async def get_channel_message(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        for op in reversed(self.message_ops):
            if str(op.get("message_id") or "") != message_id:
                continue
            if str(op.get("channel_id") or "") != channel_id:
                continue
            payload = op.get("payload")
            if isinstance(payload, dict):
                return {
                    "id": message_id,
                    "channel_id": channel_id,
                    **payload,
                }
        return {"id": message_id, "channel_id": channel_id}

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
        if message_id in self.fail_delete_message_ids:
            raise RuntimeError(f"delete failed for {message_id}")
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
    service: Optional[DiscordBotService] = field(default=None, init=False)
    _log_capture: Optional[StructuredLogCapture] = field(default=None, init=False)

    async def setup(
        self,
        *,
        agent: str = "hermes",
        approval_mode: Optional[str] = None,
    ) -> None:
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
        )
        self.service._command_runner.submit_event(event)

    def start_active_message(
        self,
        text: str,
        *,
        message_id: str = "m-2",
    ) -> asyncio.Task[None]:
        return asyncio.create_task(
            self.submit_active_message(text, message_id=message_id)
        )

    async def _run_message_inner(
        self,
        text: str,
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
            gateway_client=FakeDiscordGateway(
                [("MESSAGE_CREATE", build_discord_message_create(text))]
            ),
            state_store=self.store,
            outbox_manager=FakeDiscordOutboxManager(),
        )
        await asyncio.wait_for(
            self.service.run_forever(),
            timeout=self.timeout_seconds,
        )
        self._apply_discord_runtime_metadata(self.rest)
        return self.rest

    def start_message(
        self,
        text: str,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
    ) -> asyncio.Task[FakeDiscordRest]:
        return asyncio.create_task(
            self._run_message_inner(text, rest_client=rest_client)
        )

    async def run_message(
        self,
        text: str,
        *,
        rest_client: Optional[FakeDiscordRest] = None,
    ) -> FakeDiscordRest:
        return await self._run_message_inner(text, rest_client=rest_client)

    def _apply_discord_runtime_metadata(self, rest: FakeDiscordRest) -> None:
        service = self.service
        if service is None:
            raise RuntimeError("DiscordSurfaceHarness has no active service")
        self.rest.log_records = list(self._log_capture.records)
        self.rest.background_tasks_drained = not bool(service._background_tasks)
        self.rest.surface_key = DEFAULT_DISCORD_CHANNEL_ID
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

    async def wait_for_running_execution(
        self,
        *,
        timeout_seconds: float = 2.0,
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
        timeout_seconds: float = 2.0,
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

    async def close(self) -> None:
        if self.store is not None:
            await self.store.close()
            self.store = None
        self.service = None
        if self._log_capture is not None:
            logging.getLogger(self.logger_name).removeHandler(self._log_capture)
            self._log_capture = None


class FakeTelegramBot:
    def __init__(
        self,
        *,
        fail_delete_message_ids: Optional[set[int]] = None,
    ) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.deleted_messages: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.log_records: list[dict[str, Any]] = []
        self.surface_key: Optional[str] = None
        self.thread_target_id: Optional[str] = None
        self.execution_id: Optional[str] = None
        self.execution_status: Optional[str] = None
        self.execution_error: Optional[str] = None
        self.placeholder_message_id: Optional[int] = None
        self.placeholder_deleted: bool = False
        self.fail_delete_message_ids = set(fail_delete_message_ids or set())

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
        callback_query_id: str,
        *,
        chat_id: Optional[int] = None,
        thread_id: Optional[int] = None,
        message_id: Optional[int] = None,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        self.callback_answers.append(
            {
                "callback_query_id": callback_query_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "text": text,
                "show_alert": show_alert,
            }
        )
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
        if message_id in self.fail_delete_message_ids:
            raise RuntimeError(f"delete failed for {message_id}")
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
    _log_capture: Optional[StructuredLogCapture] = field(default=None, init=False)

    async def setup(
        self,
        *,
        agent: str = "hermes",
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
        approval_mode: Optional[str] = None,
    ) -> None:
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
        await self.service._router.ensure_topic(DEFAULT_TELEGRAM_CHAT_ID, thread_id)
        await self.service._router.update_topic(
            DEFAULT_TELEGRAM_CHAT_ID,
            thread_id,
            lambda record: _configure_telegram_pma_topic(record, agent=agent),
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
        bot_client: Optional[FakeTelegramBot] = None,
    ) -> FakeTelegramBot:
        if self.service is None:
            raise RuntimeError("TelegramSurfaceHarness.setup() must run first")
        if bot_client is not None:
            self.bot = bot_client
            self.service._bot = bot_client
        if self.bot is None:
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
        self._apply_telegram_runtime_metadata(self.bot, thread_id=thread_id)
        return self.bot

    def start_message(
        self,
        text: str,
        *,
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
        thread_id: int,
    ) -> None:
        service = self.service
        if service is None:
            raise RuntimeError("TelegramSurfaceHarness has no active service")
        bot.log_records = list(self._log_capture.records)
        bot.surface_key = f"{DEFAULT_TELEGRAM_CHAT_ID}:{thread_id}"
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
        if bot.messages:
            bot.placeholder_message_id = 1
            bot.placeholder_deleted = any(
                item.get("message_id") == 1 for item in bot.deleted_messages
            )

    async def wait_for_running_execution(
        self,
        *,
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
        timeout_seconds: float = 2.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        surface_key = f"{DEFAULT_TELEGRAM_CHAT_ID}:{thread_id}"
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
        timeout_seconds: float = 2.0,
    ) -> tuple[str, str]:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 0.0)
        surface_key = f"{DEFAULT_TELEGRAM_CHAT_ID}:{thread_id}"
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
        thread_id: int = DEFAULT_TELEGRAM_THREAD_ID,
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
