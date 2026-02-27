from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.integrations.discord.service as discord_service_module
from codex_autorunner.core.filebox import inbox_dir, outbox_pending_dir
from codex_autorunner.core.ports.run_event import (
    Completed,
    OutputDelta,
    Started,
    TokenUsage,
    ToolCall,
)
from codex_autorunner.integrations.app_server.threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    normalize_feature_key,
)
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.attachment_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.message_ops: list[dict[str, Any]] = []
        self.download_requests: list[dict[str, Any]] = []
        self.attachment_data_by_url: dict[str, bytes] = {}

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
                "payload": payload,
            }
        )

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.channel_messages.append(
            {"channel_id": channel_id, "payload": dict(payload)}
        )
        message = {"id": f"msg-{len(self.channel_messages)}"}
        self.message_ops.append(
            {
                "op": "send",
                "channel_id": channel_id,
                "payload": dict(payload),
                "message_id": message["id"],
            }
        )
        return message

    async def create_channel_message_with_attachment(
        self,
        *,
        channel_id: str,
        data: bytes,
        filename: str,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        self.attachment_messages.append(
            {
                "channel_id": channel_id,
                "data": data,
                "filename": filename,
                "caption": caption,
            }
        )
        message = {"id": f"att-{len(self.attachment_messages)}"}
        self.message_ops.append(
            {
                "op": "send_attachment",
                "channel_id": channel_id,
                "filename": filename,
                "message_id": message["id"],
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

    async def download_attachment(
        self, *, url: str, max_size_bytes: Optional[int] = None
    ) -> bytes:
        self.download_requests.append({"url": url, "max_size_bytes": max_size_bytes})
        if url not in self.attachment_data_by_url:
            raise RuntimeError(f"no attachment fixture for {url}")
        return self.attachment_data_by_url[url]

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return commands


class _FailingChannelRest(_FakeRest):
    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("simulated channel send failure")


class _FailingProgressRest(_FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise RuntimeError("simulated progress send failure")
        return await super().create_channel_message(
            channel_id=channel_id, payload=payload
        )

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("simulated progress edit failure")


class _EditFailingProgressRest(_FakeRest):
    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edit_attempts += 1
        raise RuntimeError("simulated progress edit failure")


class _DeleteFailingProgressRest(_FakeRest):
    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        _ = (channel_id, message_id)
        raise RuntimeError("simulated progress delete failure")


class _FlakyEditProgressRest(_FakeRest):
    def __init__(self, *, fail_first_edits: int) -> None:
        super().__init__()
        self.edit_attempts = 0
        self.fail_first_edits = max(0, int(fail_first_edits))

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edit_attempts += 1
        if self.edit_attempts <= self.fail_first_edits:
            raise RuntimeError("simulated transient progress edit failure")
        return await super().edit_channel_message(
            channel_id=channel_id,
            message_id=message_id,
            payload=payload,
        )


class _FakeGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for event_type, payload in self._events:
            await on_dispatch(event_type, payload)
        await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self.stopped = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


class _StreamingFakeOrchestrator:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self._thread_by_key: dict[str, str] = {}

    def get_thread_id(self, session_key: str) -> Optional[str]:
        return self._thread_by_key.get(session_key)

    def set_thread_id(self, session_key: str, thread_id: str) -> None:
        self._thread_by_key[session_key] = thread_id

    async def run_turn(
        self,
        agent_id: str,
        state: Any,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ):
        _ = (
            agent_id,
            state,
            prompt,
            model,
            reasoning,
            session_key,
            session_id,
            workspace_root,
        )
        for event in self._events:
            yield event


class _RaisingStreamingFakeOrchestrator(_StreamingFakeOrchestrator):
    def __init__(self, events: list[Any], exc: Exception) -> None:
        super().__init__(events)
        self._exc = exc

    async def run_turn(
        self,
        agent_id: str,
        state: Any,
        prompt: str,
        *,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
    ):
        _ = (
            agent_id,
            state,
            prompt,
            model,
            reasoning,
            session_key,
            session_id,
            workspace_root,
        )
        for event in self._events:
            yield event
        raise self._exc


class _FakeVoiceService:
    def __init__(self, transcript: str = "transcribed text") -> None:
        self.transcript = transcript
        self.calls: list[dict[str, Any]] = []

    async def transcribe_async(
        self, audio_bytes: bytes, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"audio_bytes": audio_bytes, **kwargs})
        return {"text": self.transcript}


def _config(
    root: Path,
    *,
    allowed_guild_ids: frozenset[str] = frozenset({"guild-1"}),
    allowed_channel_ids: frozenset[str] = frozenset({"channel-1"}),
    command_registration_enabled: bool = False,
    pma_enabled: bool = True,
    shell_enabled: bool = True,
    shell_timeout_ms: int = 120000,
    shell_max_output_chars: int = 3800,
    max_message_length: int = 2000,
    media_enabled: bool = True,
    media_voice: bool = True,
    media_max_voice_bytes: int = 10 * 1024 * 1024,
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=allowed_guild_ids,
        allowed_channel_ids=allowed_channel_ids,
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=max_message_length,
        message_overflow="split",
        pma_enabled=pma_enabled,
        shell=DiscordBotShellConfig(
            enabled=shell_enabled,
            timeout_ms=shell_timeout_ms,
            max_output_chars=shell_max_output_chars,
        ),
        media=DiscordBotMediaConfig(
            enabled=media_enabled,
            voice=media_voice,
            max_voice_bytes=media_max_voice_bytes,
        ),
    )


def _bind_interaction(path: str) -> dict[str, Any]:
    return {
        "id": "inter-bind",
        "token": "token-bind",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": "bind",
                    "options": [{"type": 3, "name": "path", "value": path}],
                }
            ],
        },
    }


def _pma_interaction(subcommand: str) -> dict[str, Any]:
    return {
        "id": "inter-pma",
        "token": "token-pma",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "pma",
            "options": [{"type": 1, "name": subcommand, "options": []}],
        },
    }


def _message_create(
    content: str = "",
    *,
    message_id: str = "m-1",
    guild_id: str = "guild-1",
    channel_id: str = "channel-1",
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": attachments or [],
    }


@pytest.mark.anyio
async def test_message_create_runs_turn_for_bound_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "Done from fake turn"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["workspace_root"] == workspace.resolve()
        assert captured[0]["prompt_text"] == "ship it"
        assert captured[0]["agent"] == "codex"
        assert captured[0]["session_key"].startswith(
            f"{FILE_CHAT_PREFIX}discord.channel-1."
        )
        assert (
            normalize_feature_key(captured[0]["session_key"])
            == captured[0]["session_key"]
        )
        assert captured[0]["orchestrator_channel_key"] == "channel-1"
        assert any(
            "Done from fake turn" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_downloads_to_inbox_and_runs_turn(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/file-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"attachment-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-1",
                            "filename": "report.txt",
                            "content_type": "text/plain",
                            "size": 16,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done with attachment"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Inbound Discord attachments:" in prompt
        assert "Outbox (pending):" in prompt
        inbox = inbox_dir(workspace.resolve())
        saved_files = [path for path in inbox.iterdir() if path.is_file()]
        assert len(saved_files) == 1
        assert saved_files[0].read_bytes() == b"attachment-bytes"
        assert str(saved_files[0]) in prompt
        assert str(outbox_pending_dir(workspace.resolve())) in prompt
        assert len(rest.download_requests) == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_and_text_keeps_text_and_adds_file_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/file-2"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"image-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="Please analyze the screenshot.",
                    attachments=[
                        {
                            "id": "att-2",
                            "filename": "screen.png",
                            "content_type": "image/png",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done with text+attachment"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert prompt.startswith("Please analyze the screenshot.")
        assert "Inbound Discord attachments:" in prompt
        assert "screen.png" in prompt
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_injects_transcript_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-1",
                            "filename": "voice-note.ogg",
                            "content_type": "audio/ogg",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("Do we have whisper support?")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done with audio"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        prompt = captured_prompts[0]
        assert "Inbound Discord attachments:" in prompt
        assert "Transcript: Do we have whisper support?" in prompt
        assert fake_voice.calls
        assert fake_voice.calls[0]["audio_bytes"] == b"voice-bytes"
        assert fake_voice.calls[0]["client"] == "discord"
        assert fake_voice.calls[0]["filename"] == "voice-note.ogg"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_audio_attachment_does_not_transcribe_when_voice_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/audio-2"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"voice-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-audio-2",
                            "filename": "voice-note.ogg",
                            "content_type": "audio/ogg",
                            "size": 11,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, media_voice=False),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    fake_voice = _FakeVoiceService("ignored")
    monkeypatch.setattr(
        service,
        "_voice_service_for_workspace",
        lambda _workspace: (fake_voice, SimpleNamespace(provider="local_whisper")),
    )

    captured_prompts: list[str] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        _ = (
            workspace_root,
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        return "Done"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert "Transcript:" not in captured_prompts[0]
        assert fake_voice.calls == []
    finally:
        await store.close()


def test_voice_service_for_workspace_uses_hub_config_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hub_config_path = tmp_path / "codex-autorunner.yml"
    hub_config_path.write_text("mode: hub\nversion: 2\n", encoding="utf-8")

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=SimpleNamespace(),
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_config_path = hub_config_path
    service._process_env = {"BASE": "1"}

    load_calls: list[tuple[Path, Optional[Path]]] = []

    def _fake_load_repo_config(
        start: Path, hub_path: Optional[Path] = None
    ) -> SimpleNamespace:
        load_calls.append((start, hub_path))
        return SimpleNamespace(
            voice={
                "enabled": False,
                "provider": "openai_whisper",
                "warn_on_remote_api": False,
            }
        )

    resolve_calls: list[tuple[Path, Optional[dict[str, str]]]] = []

    def _fake_resolve_env_for_root(
        root: Path, base_env: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        resolve_calls.append((root, base_env))
        return {
            "OPENAI_API_KEY": "workspace-key",
            "CODEX_AUTORUNNER_VOICE_ENABLED": "1",
            "CODEX_AUTORUNNER_VOICE_PROVIDER": "local_whisper",
        }

    class _StubVoiceService:
        def __init__(
            self, _voice_config: Any, logger: Any = None, env: Optional[dict] = None
        ) -> None:
            _ = logger
            self.env = dict(env or {})

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            discord_service_module, "load_repo_config", _fake_load_repo_config
        )
        monkeypatch.setattr(
            discord_service_module,
            "resolve_env_for_root",
            _fake_resolve_env_for_root,
        )
        monkeypatch.setattr(discord_service_module, "VoiceService", _StubVoiceService)

        voice_service, voice_config = service._voice_service_for_workspace(workspace)
    finally:
        monkeypatch.undo()

    assert voice_service is not None
    assert voice_config is not None
    assert load_calls == [(workspace.resolve(), hub_config_path)]
    assert resolve_calls == [(workspace.resolve(), {"BASE": "1"})]
    assert voice_config.enabled is True
    assert voice_config.provider == "local_whisper"
    assert voice_service.env.get("OPENAI_API_KEY") == "workspace-key"


def test_voice_service_for_workspace_caches_env_by_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=SimpleNamespace(),
        outbox_manager=_FakeOutboxManager(),
    )
    service._process_env = {"BASE": "1"}

    def _fake_load_repo_config(
        start: Path, hub_path: Optional[Path] = None
    ) -> SimpleNamespace:
        _ = start, hub_path
        return SimpleNamespace(
            voice={
                "enabled": True,
                "provider": "openai_whisper",
                "warn_on_remote_api": False,
            }
        )

    def _fake_resolve_env_for_root(
        root: Path, base_env: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        assert base_env == {"BASE": "1"}
        if root.resolve() == workspace_a.resolve():
            return {"OPENAI_API_KEY": "key-a"}
        return {"OPENAI_API_KEY": "key-b"}

    class _StubVoiceService:
        def __init__(
            self, _voice_config: Any, logger: Any = None, env: Optional[dict] = None
        ) -> None:
            _ = logger
            self.env = dict(env or {})

    monkeypatch.setattr(
        discord_service_module, "load_repo_config", _fake_load_repo_config
    )
    monkeypatch.setattr(
        discord_service_module,
        "resolve_env_for_root",
        _fake_resolve_env_for_root,
    )
    monkeypatch.setattr(discord_service_module, "VoiceService", _StubVoiceService)

    voice_service_a, _voice_config_a = service._voice_service_for_workspace(workspace_a)
    voice_service_b, _voice_config_b = service._voice_service_for_workspace(workspace_b)
    voice_service_a_repeat, _voice_config_a_repeat = (
        service._voice_service_for_workspace(workspace_a)
    )

    assert voice_service_a is not None
    assert voice_service_b is not None
    assert voice_service_a is voice_service_a_repeat
    assert voice_service_a.env.get("OPENAI_API_KEY") == "key-a"
    assert voice_service_b.env.get("OPENAI_API_KEY") == "key-b"


@pytest.mark.anyio
async def test_message_create_streaming_turn_posts_progress_placeholder_and_edits(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            OutputDelta(timestamp="2026-01-01T00:00:02Z", content="still thinking"),
            Completed(timestamp="2026-01-01T00:00:03Z", final_message="done"),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        send_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "send"
        ]
        edit_indices = [
            idx for idx, op in enumerate(rest.message_ops) if op.get("op") == "edit"
        ]
        assert send_indices
        assert edit_indices
        assert send_indices[0] < edit_indices[0]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_completion_sends_final_and_deletes_preview(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "done from streaming turn"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.deleted_channel_messages
        assert rest.deleted_channel_messages[0]["message_id"] == "msg-1"
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_multi_chunk_deletes_preview_and_sends_chunks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path, max_message_length=80),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "\n".join(
        [f"line {index} with enough content for chunking" for index in range(1, 20)]
    )
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.deleted_channel_messages
        assert rest.deleted_channel_messages[0]["message_id"] == "msg-1"
        final_sends = [
            op
            for op in rest.message_ops
            if op.get("op") == "send" and op.get("message_id") != "msg-1"
        ]
        assert len(final_sends) >= 2
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_appends_final_metrics(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "done from streaming turn"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            TokenUsage(
                timestamp="2026-01-01T00:00:01Z",
                usage={
                    "last": {
                        "totalTokens": 71173,
                        "inputTokens": 400,
                        "outputTokens": 245,
                    },
                    "modelContextWindow": 203352,
                },
            ),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        final_content = ""
        final_candidates = [*rest.edited_channel_messages, *rest.channel_messages]
        for message in final_candidates:
            content = str(message.get("payload", {}).get("content", ""))
            if final_text in content:
                final_content = content
                break
        assert final_content
        assert "Turn time:" in final_content
        assert "Token usage: total 71173 input 400 output 245 ctx 65%" in final_content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_ignores_user_message_delta(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    secret = "SECRET PMA CONTEXT SHOULD NOT LEAK"
    visible = "assistant output"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(
                timestamp="2026-01-01T00:00:01Z",
                content=secret,
                delta_type="user_message",
            ),
            OutputDelta(
                timestamp="2026-01-01T00:00:02Z",
                content=visible,
                delta_type="assistant_stream",
            ),
            Completed(timestamp="2026-01-01T00:00:03Z", final_message="done"),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        rendered_progress = [
            msg["payload"].get("content", "") for msg in rest.edited_channel_messages
        ]
        assert rendered_progress
        assert not any(secret in text for text in rendered_progress)
        assert any(visible in text for text in rendered_progress)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_progress_failures_are_best_effort_and_do_not_block_completion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "final despite progress failures"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.send_attempts >= 2
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_progress_edit_failures_are_best_effort_and_throttled(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _EditFailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "final despite edit failures"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            OutputDelta(timestamp="2026-01-01T00:00:02Z", content="still thinking"),
            Completed(timestamp="2026-01-01T00:00:03Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert 1 <= rest.edit_attempts <= 2
        assert rest.deleted_channel_messages
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_delete_preview_failure_still_sends_final(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _DeleteFailingProgressRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    final_text = "final despite preview delete failure"
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            Completed(timestamp="2026-01-01T00:00:02Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert any(
            final_text in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        pending = await store.list_outbox()
        assert any(
            record.operation == "delete" and record.message_id == "msg-1"
            for record in pending
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_progress_edit_recovers_after_transient_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FlakyEditProgressRest(fail_first_edits=3)
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path, max_message_length=80),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS",
        0.0,
    )
    final_text = "\n".join(
        [f"line {index} with enough content for chunking" for index in range(1, 20)]
    )
    orchestrator = _StreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
            ToolCall(
                timestamp="2026-01-01T00:00:02Z",
                tool_name="first_tool",
                tool_input={},
            ),
            ToolCall(
                timestamp="2026-01-01T00:00:03Z",
                tool_name="second_tool",
                tool_input={},
            ),
            OutputDelta(timestamp="2026-01-01T00:00:04Z", content="still thinking"),
            Completed(timestamp="2026-01-01T00:00:05Z", final_message=final_text),
        ]
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.edit_attempts >= 4
        assert rest.edited_channel_messages
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_streaming_turn_exception_marks_progress_failed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    orchestrator = _RaisingStreamingFakeOrchestrator(
        [
            Started(timestamp="2026-01-01T00:00:00Z", session_id="thread-1"),
            OutputDelta(timestamp="2026-01-01T00:00:01Z", content="thinking"),
        ],
        RuntimeError("boom"),
    )

    async def _fake_orchestrator_for_workspace(*args: Any, **kwargs: Any):
        _ = args, kwargs
        return orchestrator

    service._orchestrator_for_workspace = _fake_orchestrator_for_workspace  # type: ignore[assignment]

    try:
        await service.run_forever()
        assert rest.edited_channel_messages
        assert any(
            "failed" in msg["payload"].get("content", "")
            for msg in rest.edited_channel_messages
        )
        assert any(
            "Turn failed: boom (conversation discord:channel-1:guild-1)"
            in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_honors_shared_turn_policy_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    calls: list[tuple[str, str]] = []

    def _deny_policy(*, mode: str, context: Any) -> bool:
        calls.append((mode, context.text))
        return False

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run when shared policy denies")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.should_trigger_plain_text_turn",
        _deny_policy,
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert calls == [("always", "ship it")]
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_ignores_slash_prefixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("/car status"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("slash-prefixed text should not run message turns")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_executes_in_bound_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "car").write_text("#!/bin/sh\n", encoding="utf-8")

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!pwd"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    seen: dict[str, Any] = {}

    def _fake_shell_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, "/tmp/workspace\n", "")

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("bang-prefixed messages should bypass agent turn path")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.subprocess.run",
        _fake_shell_run,
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert seen["args"][0] == ["bash", "-lc", "pwd"]
        assert seen["kwargs"]["cwd"] == workspace.resolve()
        path_entries = seen["kwargs"]["env"]["PATH"].split(os.pathsep)
        assert str(workspace.resolve()) in path_entries
        assert any(
            "$ pwd" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert any(
            "/tmp/workspace" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_honors_shell_disable_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!ls"))])
    service = DiscordBotService(
        _config(tmp_path, shell_enabled=False),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("bang-prefixed shell command should not run agent turn")

    def _should_not_run_shell(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("shell execution should stay disabled")

    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.subprocess.run",
        _should_not_run_shell,
    )

    try:
        await service.run_forever()
        assert any(
            "Shell commands are disabled" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_bang_shell_attaches_oversized_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("!echo long"))])
    service = DiscordBotService(
        _config(tmp_path, shell_max_output_chars=12),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    long_output = "1234567890abcdefghijklmnopqrstuvwxyz\n"

    def _fake_shell_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, long_output, "")

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.subprocess.run",
        _fake_shell_run,
    )

    try:
        await service.run_forever()
        assert any(
            "$ echo long" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
        assert rest.attachment_messages
        assert rest.attachment_messages[0]["filename"].startswith("shell-output-")
        assert (
            long_output.encode("utf-8").rstrip(b"\n")
            in rest.attachment_messages[0]["data"]
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_in_pma_mode_uses_pma_session_key(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("INTERACTION_CREATE", _pma_interaction("on")),
            ("MESSAGE_CREATE", _message_create("plan next sprint")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "PMA reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["session_key"] == PMA_KEY
        assert captured[0]["orchestrator_channel_key"] == "pma:channel-1"
        assert "plan next sprint" in captured[0]["prompt_text"]
        assert captured[0]["prompt_text"] != "plan next sprint"
        assert any(
            "PMA reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_in_pma_mode_uses_hub_inbox_snapshot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
    )

    attachment_url = "https://cdn.discordapp.com/attachments/pma-zip-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"zip-bytes"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-zip-1",
                            "filename": "ticket-pack.zip",
                            "content_type": "application/zip",
                            "size": 9,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured_prompts: list[str] = []
    captured_workspaces: list[Path] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        _ = (
            agent,
            model_override,
            reasoning_effort,
            session_key,
            orchestrator_channel_key,
        )
        captured_prompts.append(prompt_text)
        captured_workspaces.append(workspace_root)
        return "PMA zip reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured_prompts
        assert captured_workspaces == [tmp_path.resolve()]

        hub_inbox = inbox_dir(tmp_path.resolve())
        hub_files = [path for path in hub_inbox.iterdir() if path.is_file()]
        assert len(hub_files) == 1
        assert hub_files[0].read_bytes() == b"zip-bytes"

        repo_inbox = inbox_dir(workspace.resolve())
        if repo_inbox.exists():
            assert [path for path in repo_inbox.iterdir() if path.is_file()] == []

        prompt = captured_prompts[0]
        assert "PMA File Inbox:" in prompt
        assert "next_action: process_uploaded_file" in prompt
        assert "ticket-pack.zip" in prompt
        assert any(
            "PMA zip reply" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_in_pma_mode_falls_back_to_hub_root_when_binding_path_invalid(
    tmp_path: Path,
) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(tmp_path / "missing-workspace"),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("status please"))])
    service = DiscordBotService(
        _config(tmp_path, allowed_channel_ids=frozenset({"channel-1"})),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        captured.append(
            {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "agent": agent,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
            }
        )
        return "fallback root ok"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        assert captured
        assert captured[0]["workspace_root"] == tmp_path.resolve()
        assert captured[0]["session_key"] == PMA_KEY
        assert any(
            "fallback root ok" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


def test_build_message_session_key_is_registry_valid(tmp_path: Path) -> None:
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=False,
        agent="codex",
    )
    opencode_key = service._build_message_session_key(
        channel_id="channel-1",
        workspace_root=workspace,
        pma_enabled=False,
        agent="opencode",
    )

    assert codex_key.startswith(f"{FILE_CHAT_PREFIX}discord.channel-1.")
    assert opencode_key.startswith(f"{FILE_CHAT_OPENCODE_PREFIX}discord.channel-1.")
    assert normalize_feature_key(codex_key) == codex_key
    assert normalize_feature_key(opencode_key) == opencode_key


@pytest.mark.anyio
async def test_message_create_denied_by_guild_allowlist(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("hello", guild_id="x"))])
    service = DiscordBotService(
        _config(
            tmp_path,
            allowed_guild_ids=frozenset({"guild-1"}),
            allowed_channel_ids=frozenset(),
        ),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert rest.channel_messages == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_resumes_paused_flow_run_in_repo_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("needs approval"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        assert text == "needs approval"
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_attachment_only_resumes_paused_flow_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    attachment_url = "https://cdn.discordapp.com/attachments/paused-file-1"
    rest = _FakeRest()
    rest.attachment_data_by_url[attachment_url] = b"paused-attachment"
    gateway = _FakeGateway(
        [
            (
                "MESSAGE_CREATE",
                _message_create(
                    content="",
                    attachments=[
                        {
                            "id": "att-paused-1",
                            "filename": "evidence.pdf",
                            "content_type": "application/pdf",
                            "size": 17,
                            "url": attachment_url,
                        }
                    ],
                ),
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    paused = SimpleNamespace(id="run-paused")
    reply_path = workspace / ".codex-autorunner" / "runs" / paused.id / "USER_REPLY.md"
    reply_path.parent.mkdir(parents=True, exist_ok=True)
    captured: dict[str, str] = {}

    async def _fake_find_paused(_: Path):
        return paused

    def _fake_write_reply(_: Path, record: Any, text: str) -> Path:
        assert record is paused
        captured["text"] = text
        reply_path.write_text(text, encoding="utf-8")
        return reply_path

    class _FakeController:
        async def resume_flow(self, run_id: str):
            assert run_id == paused.id
            return SimpleNamespace(
                id=run_id,
                status=SimpleNamespace(is_terminal=lambda: False),
            )

    async def _should_not_run_turn(
        *args: Any, **kwargs: Any
    ) -> str:  # pragma: no cover
        raise AssertionError("agent turn should not run while a paused flow is waiting")

    monkeypatch.setattr(service, "_find_paused_flow_run", _fake_find_paused)
    monkeypatch.setattr(service, "_write_user_reply", _fake_write_reply)
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(service, "_run_agent_turn_for_message", _should_not_run_turn)

    try:
        await service.run_forever()
        assert "Inbound Discord attachments:" in captured.get("text", "")
        assert "evidence.pdf" in captured.get("text", "")
        assert str(inbox_dir(workspace.resolve())) in captured.get("text", "")
        assert str(outbox_pending_dir(workspace.resolve())) in captured.get("text", "")
        assert len(rest.download_requests) == 1
        assert any(
            "resumed paused run `run-paused`" in msg["payload"].get("content", "")
            for msg in rest.channel_messages
        )
    finally:
        await store.close()


@pytest.mark.anyio
async def test_message_create_sends_queued_notice_when_dispatch_queue_is_busy(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            ("MESSAGE_CREATE", _message_create("first message", message_id="m-1")),
            ("MESSAGE_CREATE", _message_create("second message", message_id="m-2")),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    turn_count = 0

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            first_started.set()
            await release_first.wait()
            return "first reply"
        return "second reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    async def _release_later() -> None:
        await first_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()

    release_task = asyncio.create_task(_release_later())
    try:
        await asyncio.wait_for(service.run_forever(), timeout=5)
        contents = [msg["payload"].get("content", "") for msg in rest.channel_messages]
        assert any(
            "Queued (waiting for available worker...)" in content
            for content in contents
        )
        assert any("first reply" in content for content in contents)
        assert any("second reply" in content for content in contents)
    finally:
        if not release_task.done():
            release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task
        await store.close()


@pytest.mark.anyio
async def test_message_create_enqueues_outbox_when_channel_send_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    rest = _FailingChannelRest()
    gateway = _FakeGateway([("MESSAGE_CREATE", _message_create("ship it"))])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    async def _fake_run_turn(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> str:
        return "queued reply"

    service._run_agent_turn_for_message = _fake_run_turn.__get__(
        service, DiscordBotService
    )

    try:
        await service.run_forever()
        outbox = await store.list_outbox()
        assert outbox
        assert any(
            "queued reply" in item.payload_json.get("content", "") for item in outbox
        )
    finally:
        await store.close()
