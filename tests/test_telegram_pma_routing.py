import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.handlers.commands.execution import (
    ExecutionCommands,
    _TurnRunResult,
)
from codex_autorunner.integrations.telegram.handlers.messages import (
    handle_media_message,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord) -> None:
        self._record = record

    async def get_topic(self, _key: str) -> TelegramTopicRecord:
        return self._record


class _ExecutionStub(ExecutionCommands):
    def __init__(self, record: TelegramTopicRecord, hub_root: Path) -> None:
        self._logger = logging.getLogger("test")
        self._router = _RouterStub(record)
        self._hub_root = hub_root
        self._hub_supervisor = None
        self._hub_thread_registry = None
        self._turn_semaphore = asyncio.Semaphore(1)
        self._captured: dict[str, object] = {}
        self._config = SimpleNamespace(
            agent_turn_timeout_seconds={"codex": None, "opencode": None}
        )

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        return self._turn_semaphore

    async def _prepare_turn_placeholder(
        self,
        message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
        return None

    async def _execute_codex_turn(
        self,
        message: TelegramMessage,
        runtime: object,
        record: TelegramTopicRecord,
        prompt_text: str,
        thread_id: Optional[str],
        key: str,
        turn_semaphore: asyncio.Semaphore,
        input_items: Optional[list[dict[str, object]]],
        *,
        placeholder_id: Optional[int],
        placeholder_text: str,
        send_failure_response: bool,
        allow_new_thread: bool,
        missing_thread_message: Optional[str],
        transcript_message_id: Optional[int],
        transcript_text: Optional[str],
        pma_thread_registry: Optional[object] = None,
        pma_thread_key: Optional[str] = None,
    ) -> _TurnRunResult:
        self._captured["prompt_text"] = prompt_text
        self._captured["workspace_path"] = record.workspace_path
        return _TurnRunResult(
            record=record,
            thread_id=thread_id,
            turn_id="turn-1",
            response="ok",
            placeholder_id=None,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=None,
            transcript_text=None,
        )

    def _effective_agent(self, _record: TelegramTopicRecord) -> str:
        return "codex"


@pytest.mark.anyio
async def test_pma_prompt_routing_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    prompt_path = hub_root / ".codex-autorunner" / "pma" / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("PMA system prompt", encoding="utf-8")

    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    handler = _ExecutionStub(record, hub_root)
    message = TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=123,
        thread_id=None,
        from_user_id=456,
        text="hello",
        date=None,
        is_topic_message=False,
    )

    result = await handler._run_turn_and_collect_result(
        message,
        runtime=SimpleNamespace(),
        text_override=None,
        send_placeholder=False,
    )

    assert isinstance(result, _TurnRunResult)
    assert handler._captured["workspace_path"] == str(hub_root)
    prompt_text = handler._captured["prompt_text"]
    assert "<hub_snapshot>" in prompt_text
    assert "<user_message>" in prompt_text
    assert "hello" in prompt_text


@pytest.mark.anyio
async def test_pma_media_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    sent: list[str] = []
    captured: dict[str, object] = {}

    class _MediaRouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

    class _MediaHandlerStub:
        def __init__(self) -> None:
            self._hub_root = hub_root
            self._router = _MediaRouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                ),
                ticket_flow_auto_resume=False,
            )
            self._ticket_flow_pause_targets = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *_, **__: None
            )
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            sent.append(text)

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _handle_file_message(
            self,
            message: TelegramMessage,
            runtime: object,
            record_arg: TelegramTopicRecord,
            candidate: object,
            caption_text: str,
            *,
            placeholder_id: Optional[int] = None,
        ) -> None:
            captured["workspace_path"] = record_arg.workspace_path
            captured["caption"] = caption_text
            captured["kind"] = "file"

    handler = _MediaHandlerStub()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        document=TelegramDocument(
            file_id="file-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=10,
        ),
        caption="please review",
    )
    await handle_media_message(
        handler, message, runtime=object(), caption_text="please review"
    )

    assert not sent  # no "Topic not bound" error
    assert captured["workspace_path"] == str(hub_root)
    assert captured["caption"] == "please review"
    assert captured["kind"] == "file"


@pytest.mark.anyio
async def test_pma_voice_uses_hub_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    hub_root.mkdir(parents=True, exist_ok=True)
    record = TelegramTopicRecord(pma_enabled=True, workspace_path=None)
    sent: list[str] = []
    captured: dict[str, object] = {}

    class _VoiceRouterStub:
        async def get_topic(self, _key: str) -> TelegramTopicRecord:
            return record

    class _VoiceHandlerStub:
        def __init__(self) -> None:
            self._hub_root = hub_root
            self._router = _VoiceRouterStub()
            self._logger = logging.getLogger("test")
            self._config = SimpleNamespace(
                media=SimpleNamespace(
                    enabled=True,
                    images=True,
                    voice=True,
                    files=True,
                    max_image_bytes=10_000_000,
                    max_voice_bytes=10_000_000,
                    max_file_bytes=10_000_000,
                ),
                ticket_flow_auto_resume=False,
            )
            self._ticket_flow_pause_targets = {}
            self._ticket_flow_bridge = SimpleNamespace(
                auto_resume_run=lambda *_, **__: None
            )
            self._bot_username = None

        async def _resolve_topic_key(
            self, chat_id: int, thread_id: Optional[int]
        ) -> str:
            return f"{chat_id}:{thread_id}"

        async def _send_message(
            self,
            _chat_id: int,
            text: str,
            *,
            thread_id: Optional[int],
            reply_to: Optional[int],
        ) -> None:
            sent.append(text)

        def _get_paused_ticket_flow(
            self, _workspace_root: Path, *, preferred_run_id: Optional[str]
        ) -> Optional[tuple[str, object]]:
            return None

        async def _handle_voice_message(
            self,
            message: TelegramMessage,
            runtime: object,
            record_arg: TelegramTopicRecord,
            candidate: object,
            caption_text: str,
            *,
            placeholder_id: Optional[int] = None,
        ) -> None:
            captured["workspace_path"] = record_arg.workspace_path
            captured["caption"] = caption_text
            captured["kind"] = "voice"

    handler = _VoiceHandlerStub()
    message = TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=111,
        thread_id=222,
        from_user_id=333,
        text=None,
        date=None,
        is_topic_message=True,
        voice=TelegramVoice("voice-1", None, 3, "audio/ogg", 100),
        caption="voice note",
    )
    await handle_media_message(
        handler, message, runtime=object(), caption_text="voice note"
    )

    assert not sent  # no "Topic not bound" error
    assert captured["workspace_path"] == str(hub_root)
    assert captured["caption"] == "voice note"
    assert captured["kind"] == "voice"
