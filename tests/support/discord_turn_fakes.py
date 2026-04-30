from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import codex_autorunner.agents.registry as agent_registry_module
from codex_autorunner.agents.registry import AgentDescriptor
from codex_autorunner.integrations.chat.collaboration_policy import CollaborationPolicy
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotDispatchConfig,
    DiscordBotMediaConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.original_interaction_edits: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.attachment_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
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
        self.original_interaction_edits.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": dict(payload),
            }
        )
        return {"id": "original-response"}

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

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)

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


def _latest_interaction_completion_payload(rest: _FakeRest) -> dict[str, Any]:
    if rest.original_interaction_edits:
        return rest.original_interaction_edits[-1]["payload"]
    if rest.followup_messages:
        return rest.followup_messages[-1]["payload"]
    raise AssertionError("expected an interaction completion payload")


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


class _FakeCompactHubClient:
    def __init__(
        self,
        orchestration_service: Any,
        *,
        transcript_entries: Optional[list[dict[str, str]]] = None,
    ) -> None:
        self._orchestration_service = orchestration_service
        self._transcript_entries = list(transcript_entries or [])
        self.transcript_requests: list[Any] = []
        self.compact_seed_updates: list[Any] = []

    async def get_transcript_history(self, request: Any) -> Any:
        self.transcript_requests.append(request)
        return SimpleNamespace(entries=list(self._transcript_entries))

    async def update_thread_compact_seed(self, request: Any) -> Any:
        self.compact_seed_updates.append(request)
        thread_store = getattr(self._orchestration_service, "thread_store", None)
        remote_client = getattr(thread_store, "_client", None)
        if remote_client is not None and hasattr(
            remote_client, "update_thread_compact_seed"
        ):
            return await remote_client.update_thread_compact_seed(request)
        pma_store = getattr(thread_store, "_store", None)
        if pma_store is not None:
            pma_store.set_thread_compact_seed(
                request.thread_target_id,
                request.compact_seed,
            )
        return SimpleNamespace(
            thread_target_id=request.thread_target_id,
            compact_seed=request.compact_seed,
        )


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
        input_items: Optional[list[dict[str, Any]]] = None,
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
            input_items,
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
        input_items: Optional[list[dict[str, Any]]] = None,
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
            input_items,
            model,
            reasoning,
            session_key,
            session_id,
            workspace_root,
        )
        for event in self._events:
            yield event
        raise self._exc


class _StreamingFakeHarness:
    display_name = "StreamingFake"
    capabilities = frozenset(
        {
            "durable_threads",
            "message_turns",
            "interrupt",
            "event_streaming",
        }
    )

    def __init__(
        self,
        events: list[Any],
        *,
        status: str = "ok",
        assistant_text: str = "done",
        errors: Optional[list[str]] = None,
        wait_for_stream: bool = False,
        stream_exception: Optional[Exception] = None,
        allow_parallel_event_stream: bool = True,
    ) -> None:
        self._events = events
        self._status = status
        self._assistant_text = assistant_text
        self._errors = list(errors or [])
        self._wait_for_stream = wait_for_stream
        self._stream_exception = stream_exception
        if allow_parallel_event_stream:
            self.capabilities = type(self).capabilities
        else:
            self.capabilities = frozenset(
                capability
                for capability in type(self).capabilities
                if capability != "event_streaming"
            )
        self._stream_done = asyncio.Event()

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> SimpleNamespace:
        _ = workspace_root, title
        return SimpleNamespace(id="backend-thread-1")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> SimpleNamespace:
        _ = workspace_root
        return SimpleNamespace(id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> SimpleNamespace:
        _ = (
            workspace_root,
            model,
            reasoning,
            approval_mode,
            sandbox_policy,
            input_items,
        )
        return SimpleNamespace(
            conversation_id=conversation_id, turn_id="backend-turn-1"
        )

    async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        raise AssertionError("review mode should not be used in this test")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = workspace_root, conversation_id, timeout
        assert isinstance(turn_id, str)
        if self._wait_for_stream:
            await self._stream_done.wait()
        return SimpleNamespace(
            status=self._status,
            assistant_text=self._assistant_text,
            errors=list(self._errors),
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id, turn_id

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id, turn_id
        try:
            for event in self._events:
                if (
                    isinstance(event, tuple)
                    and len(event) == 2
                    and isinstance(event[0], (int, float))
                ):
                    delay = float(event[0])
                    payload = event[1]
                    if delay > 0:
                        await asyncio.sleep(delay)
                    yield payload
                    continue
                yield event
            if self._stream_exception is not None:
                raise self._stream_exception
        finally:
            self._stream_done.set()


def _patch_streaming_harness(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Any],
    *,
    agent_id: str = "codex",
    status: str = "ok",
    assistant_text: str = "done",
    errors: Optional[list[str]] = None,
    wait_for_stream: bool = False,
    stream_exception: Optional[Exception] = None,
    allow_parallel_event_stream: bool = True,
) -> _StreamingFakeHarness:
    harness = _StreamingFakeHarness(
        events,
        status=status,
        assistant_text=assistant_text,
        errors=errors,
        wait_for_stream=wait_for_stream,
        stream_exception=stream_exception,
        allow_parallel_event_stream=allow_parallel_event_stream,
    )
    monkeypatch.setattr(
        agent_registry_module,
        "get_registered_agents",
        lambda context=None: {
            agent_id: AgentDescriptor(
                id=agent_id,
                name=agent_id.title(),
                capabilities=harness.capabilities,
                make_harness=lambda _ctx: harness,
            )
        },
    )
    return harness


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
    message_overflow: str = "split",
    media_enabled: bool = True,
    media_voice: bool = True,
    media_max_voice_bytes: int = 10 * 1024 * 1024,
    collaboration_policy: CollaborationPolicy | None = None,
    ack_budget_ms: int = 10_000,
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
        message_overflow=message_overflow,
        pma_enabled=pma_enabled,
        dispatch=DiscordBotDispatchConfig(ack_budget_ms=ack_budget_ms),
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
        collaboration_policy=collaboration_policy,
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
    extra_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "content": content,
        "author": {"id": "user-1", "bot": False},
        "attachments": attachments or [],
    }
    if extra_payload:
        payload.update(extra_payload)
    return payload


class _InteractionFakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.command_sync_calls: list[dict[str, Any]] = []
        self.fetched_channel_messages: dict[tuple[str, str], dict[str, Any]] = {}
        self._typing_event: asyncio.Event | None = None

    def _new_typing_event(self) -> asyncio.Event:
        self._typing_event = asyncio.Event()
        return self._typing_event

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
        message_id = f"msg-{len(self.channel_messages) + 1}"
        self.channel_messages.append(
            {
                "channel_id": channel_id,
                "payload": payload,
                "message_id": message_id,
            }
        )
        return {"id": message_id, "channel_id": channel_id, "payload": payload}

    async def get_channel_message(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any]:
        return dict(self.fetched_channel_messages.get((channel_id, message_id), {}))

    async def edit_channel_message(
        self, *, channel_id: str, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.edited_channel_messages.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "payload": payload,
            }
        )
        return {"id": message_id}

    async def delete_channel_message(self, *, channel_id: str, message_id: str) -> None:
        self.deleted_channel_messages.append(
            {"channel_id": channel_id, "message_id": message_id}
        )

    async def trigger_typing(self, *, channel_id: str) -> None:
        self.typing_calls.append(channel_id)
        if self._typing_event is not None:
            self._typing_event.set()

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
                "payload": payload,
            }
        )
        return {"id": "followup-1"}

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
                "payload": payload,
            }
        )
        return {"id": "@original"}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.command_sync_calls.append(
            {
                "application_id": application_id,
                "guild_id": guild_id,
                "commands": commands,
            }
        )
        return commands


class _InteractionFakeGateway:
    def __init__(
        self, events: list[dict[str, Any] | tuple[str, dict[str, Any]]]
    ) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        for item in self._events:
            if isinstance(item, tuple):
                event_type, payload = item
            else:
                event_type, payload = "INTERACTION_CREATE", item
            await on_dispatch(event_type, payload)

    async def stop(self) -> None:
        self.stopped = True


def _latest_public_response_payload(rest: _InteractionFakeRest) -> dict[str, Any]:
    if rest.edited_original_interaction_responses:
        return rest.edited_original_interaction_responses[-1]["payload"]
    if rest.followup_messages:
        return rest.followup_messages[-1]["payload"]
    raise AssertionError("expected a Discord public response payload")


def _interaction(
    *,
    name: str,
    options: list[dict[str, Any]],
    user_id: str = "user-1",
    interaction_id: str = "inter-1",
    interaction_token: str = "token-1",
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "token": interaction_token,
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [{"type": 1, "name": name, "options": options}],
        },
    }


def _interaction_path(
    *,
    command_path: tuple[str, ...],
    options: list[dict[str, Any]],
    user_id: str = "user-1",
) -> dict[str, Any]:
    assert command_path and command_path[0] == "car"
    if len(command_path) == 2:
        return _interaction(name=command_path[1], options=options, user_id=user_id)
    if len(command_path) == 3:
        return {
            "id": "inter-1",
            "token": "token-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "member": {"user": {"id": user_id}},
            "data": {
                "name": "car",
                "options": [
                    {
                        "type": 2,
                        "name": command_path[1],
                        "options": [
                            {
                                "type": 1,
                                "name": command_path[2],
                                "options": options,
                            }
                        ],
                    }
                ],
            },
        }
    raise AssertionError(f"Unsupported command path for test helper: {command_path}")


def _autocomplete_interaction(
    *,
    name: str,
    focused_name: str,
    focused_value: str,
    user_id: str = "user-1",
) -> dict[str, Any]:
    return {
        "id": "inter-autocomplete-1",
        "token": "token-autocomplete-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 4,
        "member": {"user": {"id": user_id}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 1,
                    "name": name,
                    "options": [
                        {
                            "type": 3,
                            "name": focused_name,
                            "value": focused_value,
                            "focused": True,
                        }
                    ],
                }
            ],
        },
    }


def _autocomplete_interaction_path(
    *,
    command_path: tuple[str, ...],
    focused_name: str,
    focused_value: str,
    user_id: str = "user-1",
) -> dict[str, Any]:
    assert command_path and command_path[0] == "car"
    if len(command_path) == 2:
        return _autocomplete_interaction(
            name=command_path[1],
            focused_name=focused_name,
            focused_value=focused_value,
            user_id=user_id,
        )
    if len(command_path) == 3:
        return {
            "id": "inter-autocomplete-1",
            "token": "token-autocomplete-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "type": 4,
            "member": {"user": {"id": user_id}},
            "data": {
                "name": "car",
                "options": [
                    {
                        "type": 2,
                        "name": command_path[1],
                        "options": [
                            {
                                "type": 1,
                                "name": command_path[2],
                                "options": [
                                    {
                                        "type": 3,
                                        "name": focused_name,
                                        "value": focused_value,
                                        "focused": True,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        }
    raise AssertionError(f"Unsupported command path for test helper: {command_path}")


def _component_interaction(
    *, custom_id: str | None, values: list[Any] | None = None, user_id: str = "user-1"
) -> dict[str, Any]:
    data: dict[str, Any] = {"component_type": 3}
    if custom_id is not None:
        data["custom_id"] = custom_id
    if values is not None:
        data["values"] = values
    return {
        "id": "inter-component-1",
        "token": "token-component-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 3,
        "member": {"user": {"id": user_id}},
        "data": data,
    }


async def _dispatch_gateway_interaction(
    service: DiscordBotService,
    payload: dict[str, Any],
) -> None:
    await service._on_dispatch("INTERACTION_CREATE", payload)
    await asyncio.wait_for(service._command_runner.shutdown(), timeout=3.0)


async def _build_discord_service(
    tmp_path: Path,
    gateway_events: list[tuple[str, dict[str, Any]]],
    *,
    rest: Any | None = None,
    config_kwargs: dict[str, Any] | None = None,
) -> tuple[DiscordBotService, Any, DiscordStateStore, Path]:
    import logging

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
    if rest is None:
        rest = _FakeRest()
    gateway = _FakeGateway(gateway_events)
    kwargs = config_kwargs or {}
    service = DiscordBotService(
        _config(tmp_path, **kwargs),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    return service, rest, store, workspace
