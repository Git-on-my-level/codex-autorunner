import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCallbackQuery,
    TelegramDocument,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramPhotoSize,
)
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.handlers import (
    commands_runtime as telegram_commands_runtime,
)
from codex_autorunner.integrations.telegram.service import TelegramBotService
from codex_autorunner.integrations.telegram.types import PendingQuestion, SelectionState

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_server_fixture.py"

pytestmark = pytest.mark.integration


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


def make_config(
    root: Path,
    command: list[str],
    overrides: Optional[dict[str, object]] = None,
    *,
    collaboration_raw: Optional[dict[str, object]] = None,
) -> TelegramBotConfig:
    raw = {
        "enabled": True,
        "mode": "polling",
        "allowed_chat_ids": [123],
        "allowed_user_ids": [456],
        "require_topics": False,
        "app_server_command": command,
    }
    if overrides:
        raw.update(overrides)
    env = {
        "CAR_TELEGRAM_BOT_TOKEN": "test-token",
        "CAR_TELEGRAM_CHAT_ID": "123",
    }
    return TelegramBotConfig.from_raw(
        raw,
        root=root,
        env=env,
        collaboration_raw=collaboration_raw,
    )


def build_message(
    text: str,
    *,
    chat_id: int = 123,
    thread_id: Optional[int] = None,
    user_id: int = 456,
    message_id: int = 1,
    update_id: int = 1,
    **kwargs: object,
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=user_id,
        text=text,
        date=0,
        is_topic_message=thread_id is not None,
        **kwargs,
    )


def build_document_message(
    document: TelegramDocument,
    *,
    chat_id: int = 123,
    thread_id: Optional[int] = None,
    user_id: int = 456,
    message_id: int = 1,
    update_id: int = 1,
    caption: Optional[str] = None,
    **kwargs: object,
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=user_id,
        text=None,
        caption=caption,
        date=0,
        is_topic_message=thread_id is not None,
        document=document,
        **kwargs,
    )


def build_photo_message(
    photos: tuple[TelegramPhotoSize, ...],
    *,
    chat_id: int = 123,
    thread_id: Optional[int] = None,
    user_id: int = 456,
    message_id: int = 1,
    update_id: int = 1,
    caption: Optional[str] = None,
    **kwargs: object,
) -> TelegramMessage:
    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        thread_id=thread_id,
        from_user_id=user_id,
        text=None,
        caption=caption,
        date=0,
        is_topic_message=thread_id is not None,
        photos=photos,
        **kwargs,
    )


def build_service_in_closed_loop(
    tmp_path: Path, config: TelegramBotConfig
) -> TelegramBotService:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return TelegramBotService(config, hub_root=tmp_path)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


async def _drain_spawned_tasks(service: TelegramBotService) -> None:
    # Per-topic work may run on TopicQueue workers (see concurrency.per_topic_queue)
    # rather than service._spawned_tasks; drain both until stable.
    while True:
        while service._spawned_tasks:
            await asyncio.gather(*tuple(service._spawned_tasks))
        for runtime in service._router._topics.values():
            await runtime.queue.join_idle()
        if not service._spawned_tasks:
            return


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.documents: list[dict[str, object]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        message_thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        self.messages.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "text": text,
                "reply_to": reply_to_message_id,
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
        reply_markup: Optional[dict[str, object]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
        max_len: int = 4096,
    ) -> list[dict[str, object]]:
        self.messages.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "text": text,
                "reply_to": reply_to_message_id,
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
    ) -> dict[str, object]:
        self.documents.append(
            {
                "chat_id": chat_id,
                "thread_id": message_thread_id,
                "reply_to": reply_to_message_id,
                "filename": filename,
                "caption": caption,
                "bytes_len": len(document),
            }
        )
        return {"message_id": len(self.documents)}

    async def answer_callback_query(
        self,
        _callback_query_id: str,
        *,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> dict[str, object]:
        return {}

    async def edit_message_text(
        self,
        _chat_id: int,
        _message_id: int,
        _text: str,
        *,
        reply_markup: Optional[dict[str, object]] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, object]:
        return {}


@pytest.mark.anyio
async def test_begin_typing_indicator_rolls_back_when_spawn_fails(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)

    def _raise_spawn_error(_coro: object) -> object:
        raise RuntimeError("spawn failed")

    service._spawn_task = _raise_spawn_error  # type: ignore[assignment]

    try:
        with pytest.raises(RuntimeError, match="spawn failed"):
            await service._begin_typing_indicator(123, None)

        key = (123, None)
        assert key not in service._typing_tasks
        assert service._typing_sessions.get(key, 0) == 0
    finally:
        await service._app_server_supervisor.close_all()


@pytest.mark.anyio
async def test_status_creates_record(tmp_path: Path) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    message = build_message("/status", thread_id=55)
    try:
        await service._handle_status(message)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.messages
    text = fake_bot.messages[-1]["text"]
    assert "Workspace: unbound" in text
    assert "Topic not bound" not in text
    key = await service._router.resolve_key(message.chat_id, message.thread_id)
    record = await service._router.get_topic(key)
    assert record is not None


@pytest.mark.anyio
async def test_unbound_topic_plain_text_stays_quiet_until_activation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    message = build_message(
        "hello team",
        thread_id=55,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
    )
    try:
        with caplog.at_level(logging.INFO):
            await service._handle_message_inner(message)
            await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.messages == []
    assert "telegram.collaboration_policy.evaluated" in caplog.text
    assert '"policy_outcome":"command_only_destination"' in caplog.text
    assert "Topic not bound" not in caplog.text


@pytest.mark.anyio
async def test_status_reports_collaboration_policy_for_topic(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 55,
                        "name": "ops",
                        "mode": "command_only",
                        "plain_text_trigger": "disabled",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    message = build_message(
        "/status",
        thread_id=55,
        chat_type="supergroup",
        thread_title="Ops",
    )
    try:
        await service._handle_status(message)
    finally:
        await service._app_server_supervisor.close_all()
    text = fake_bot.messages[-1]["text"]
    assert "Policy destination: ops" in text
    assert "Policy mode: command_only" in text
    assert "Policy plain-text: disabled" in text


@pytest.mark.anyio
async def test_ids_reports_collaboration_snippet_for_topic(tmp_path: Path) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    message = build_message(
        "/ids",
        thread_id=77,
        chat_type="supergroup",
        thread_title="Planning",
    )
    try:
        await service._handle_ids(message)
    finally:
        await service._app_server_supervisor.close_all()
    text = fake_bot.messages[-1]["text"]
    assert "Topic key: 123:77" in text
    assert "Suggested collaboration config:" in text
    assert "collaboration_policy:" in text
    assert "require_topics: true" in text
    assert "thread_id: 77" in text
    assert "mode: active" in text
    assert "mode: silent" in text


@pytest.mark.anyio
async def test_normal_message_runs_turn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("Bound to" in msg["text"] for msg in fake_bot.messages)
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_require_topics_blocks_root_chat_commands(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        overrides={"require_topics": True},
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    message = build_message("/status", chat_type="supergroup")
    try:
        await service._handle_message(message)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.messages == []


@pytest.mark.anyio
async def test_command_only_topic_allows_commands_but_ignores_plain_text(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "command_only",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    status_message = build_message(
        "/status",
        thread_id=77,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
        entities=(TelegramMessageEntity(type="bot_command", offset=0, length=7),),
    )
    plain_message = build_message(
        "hello team",
        thread_id=77,
        message_id=11,
        update_id=11,
        chat_type="supergroup",
    )
    try:
        await service._handle_message_inner(status_message)
        await _drain_spawned_tasks(service)
        count_after_status = len(fake_bot.messages)
        await service._handle_message_inner(plain_message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert count_after_status > 0
    assert "Policy mode: command_only" in fake_bot.messages[-1]["text"]
    assert len(fake_bot.messages) == count_after_status
    assert not any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_silent_topic_ignores_commands_and_plain_text(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "silent",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    try:
        await service._handle_message(
            build_message(
                "/status",
                thread_id=77,
                message_id=10,
                update_id=10,
                chat_type="supergroup",
                entities=(
                    TelegramMessageEntity(type="bot_command", offset=0, length=7),
                ),
            )
        )
        await _drain_spawned_tasks(service)
        await service._handle_message(
            build_message(
                "hello team",
                thread_id=77,
                message_id=11,
                update_id=11,
                chat_type="supergroup",
            )
        )
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.messages == []


@pytest.mark.anyio
async def test_mentions_only_topic_requires_invocation_before_turn_starts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "active",
                        "plain_text_trigger": "mentions",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    service._bot_username = "TestBot"
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", thread_id=77, message_id=10, update_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        before = len(fake_bot.messages)
        await service._handle_message_inner(
            build_message(
                "hello team",
                thread_id=77,
                message_id=11,
                update_id=11,
                chat_type="supergroup",
            )
        )
        await _drain_spawned_tasks(service)
        after_plain_text = len(fake_bot.messages)
        await service._handle_message_inner(
            build_message(
                "@TestBot hello team",
                thread_id=77,
                message_id=12,
                update_id=12,
                chat_type="supergroup",
            )
        )
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert before == after_plain_text
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_private_chat_stays_easy_with_mentions_trigger(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        overrides={"trigger_mode": "mentions"},
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    dm_message = build_message("hello from dm", message_id=11, chat_type="private")
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_message_inner(dm_message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_private_chat_default_path_runs_without_collaboration_policy(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10, chat_type="private")
    dm_message = build_message("hello from dm", message_id=11, chat_type="private")
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_message_inner(dm_message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_bound_media_respects_mentions_trigger_before_turn_starts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        overrides={"trigger_mode": "mentions"},
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    media_message = build_photo_message(
        (TelegramPhotoSize("p1", None, 800, 600, 3),),
        thread_id=77,
        message_id=11,
        update_id=11,
        chat_type="supergroup",
    )
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_message_inner(media_message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert len(fake_bot.messages) == 1
    assert "Bound to" in fake_bot.messages[0]["text"]
    assert not any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_silent_topic_blocks_pending_bind_reply(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "silent",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    key = await service._resolve_topic_key(123, 77)
    service._bind_options[key] = SelectionState(
        items=[("repo-basic", "repo-basic")],
        requester_user_id="456",
    )
    message = build_message(
        "1",
        thread_id=77,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
    )
    try:
        await service._handle_message_inner(message)
        await _drain_spawned_tasks(service)
        record = await service._router.get_topic(key)
    finally:
        await service._app_server_supervisor.close_all()
    assert service._bind_options[key].items == [("repo-basic", "repo-basic")]
    assert record is None or record.workspace_path is None
    assert fake_bot.messages == []


@pytest.mark.anyio
async def test_pending_bind_reply_requires_original_user(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        overrides={"trigger_mode": "disabled"},
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "command_only",
                    }
                ],
                "allowed_user_ids": [456, 789],
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    key = await service._resolve_topic_key(123, 77)
    service._bind_options[key] = SelectionState(
        items=[("repo-basic", "repo-basic")],
        requester_user_id="456",
    )
    message = build_message(
        "1",
        thread_id=77,
        user_id=789,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
    )
    try:
        await service._handle_message_inner(message)
        await _drain_spawned_tasks(service)
        record = await service._router.get_topic(key)
    finally:
        await service._app_server_supervisor.close_all()
    assert service._bind_options[key].items == [("repo-basic", "repo-basic")]
    assert record is None or record.workspace_path is None
    assert fake_bot.messages == []


@pytest.mark.anyio
async def test_silent_topic_blocks_pending_custom_question_reply(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        collaboration_raw={
            "telegram": {
                "destinations": [
                    {
                        "chat_id": 123,
                        "thread_id": 77,
                        "mode": "silent",
                    }
                ]
            }
        },
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str | None] = loop.create_future()
    service._pending_questions["req-1"] = PendingQuestion(
        request_id="req-1",
        turn_id="turn-1",
        codex_thread_id=None,
        chat_id=123,
        thread_id=77,
        topic_key="123:77",
        requester_user_id="456",
        message_id=99,
        created_at="now",
        question_index=0,
        prompt="Need input",
        options=[],
        future=future,
        awaiting_custom_input=True,
    )
    message = build_message(
        "custom answer",
        thread_id=77,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
    )
    try:
        await service._handle_message(message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert "req-1" in service._pending_questions
    assert future.done() is False
    assert fake_bot.messages == []


@pytest.mark.anyio
async def test_denied_user_is_ignored_and_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    denied_message = build_message(
        "hello",
        user_id=999,
        message_id=10,
        update_id=10,
        chat_type="supergroup",
    )
    caplog.set_level(logging.INFO)
    try:
        await service._handle_message_inner(denied_message)
        await _drain_spawned_tasks(service)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.messages == []
    assert "telegram.collaboration_policy.evaluated" in caplog.text
    assert '"policy_outcome":"denied_actor"' in caplog.text


@pytest.mark.anyio
async def test_document_message_saves_inbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)

    async def fake_download(
        _file_id: str, *, max_bytes: Optional[int] = None
    ) -> tuple[bytes, str, int]:
        return b"data", "files/report.txt", 4

    service._download_telegram_file = fake_download
    document = TelegramDocument("d1", None, "report.txt", "text/plain", 4)
    message = build_document_message(document, message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        runtime = service._router.runtime_for(
            await service._router.resolve_key(
                bind_message.chat_id, bind_message.thread_id
            )
        )
        await service._handle_media_message(message, runtime, "")
    finally:
        await service._app_server_supervisor.close_all()
    inbox_root = repo / ".codex-autorunner" / "uploads" / "telegram-files"
    inbox_files = [path for path in inbox_root.rglob("*") if path.is_file()]
    assert inbox_files


@pytest.mark.anyio
async def test_photo_batch_message_saves_inbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)

    async def fake_download(
        _file_id: str, *, max_bytes: Optional[int] = None
    ) -> tuple[bytes, str, int]:
        return b"img", "photos/sample.jpg", 3

    service._download_telegram_file = fake_download
    message = build_photo_message(
        (TelegramPhotoSize("p1", None, 800, 600, 3),),
        message_id=11,
    )
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_media_batch([message])
    finally:
        await service._app_server_supervisor.close_all()
    inbox_root = repo / ".codex-autorunner" / "uploads" / "telegram-files"
    inbox_files = [path for path in inbox_root.rglob("*") if path.is_file()]
    assert inbox_files
    assert any(path.suffix.lower() == ".jpg" for path in inbox_files)


@pytest.mark.anyio
async def test_photo_batch_inbox_save_failure_still_processes_image(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)

    async def fake_download(
        _file_id: str, *, max_bytes: Optional[int] = None
    ) -> tuple[bytes, str, int]:
        return b"img", "photos/sample.jpg", 3

    async def fake_handle_normal(
        _message: TelegramMessage,
        _runtime: object,
        *,
        text_override: Optional[str] = None,
        input_items: Optional[list[dict[str, object]]] = None,
        record: Optional[object] = None,
        send_placeholder: bool = True,
        transcript_message_id: Optional[int] = None,
        transcript_text: Optional[str] = None,
        placeholder_id: Optional[int] = None,
    ) -> None:
        captured["text_override"] = text_override
        captured["input_items"] = input_items

    def fail_inbox_save(*_args: object, **_kwargs: object) -> Path:
        raise OSError("simulated inbox write failure")

    captured: dict[str, object] = {}
    service._download_telegram_file = fake_download
    service._handle_normal_message = fake_handle_normal  # type: ignore[assignment]
    service._save_inbox_file = fail_inbox_save  # type: ignore[assignment]
    message = build_photo_message(
        (TelegramPhotoSize("p1", None, 800, 600, 3),),
        message_id=11,
    )
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_media_batch([message])
    finally:
        await service._app_server_supervisor.close_all()

    prompt_text = captured.get("text_override")
    assert isinstance(prompt_text, str)
    assert "Failed to process 1 item(s)." not in prompt_text
    input_items = captured.get("input_items")
    assert isinstance(input_items, list)
    assert any(
        isinstance(item, dict) and item.get("type") == "localImage"
        for item in input_items
    )
    image_root = repo / ".codex-autorunner" / "uploads" / "telegram-images"
    image_files = [path for path in image_root.rglob("*") if path.is_file()]
    assert image_files


@pytest.mark.anyio
async def test_outbox_pending_file_sent_after_turn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        pending_dir = service._files_outbox_pending_dir(str(repo), key)
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_file = pending_dir / "report.txt"
        pending_file.write_text("hello", encoding="utf-8")
        runtime = service._router.runtime_for(key)
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert fake_bot.documents
    assert fake_bot.documents[-1]["filename"] == "report.txt"
    assert not pending_file.exists()
    sent_dir = service._files_outbox_sent_dir(str(repo), key)
    sent_files = [path for path in sent_dir.iterdir() if path.is_file()]
    assert sent_files


@pytest.mark.anyio
async def test_error_notification_surfaces(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("turn_error_no_agent"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("Auth required" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_bang_shell_attaches_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(
        tmp_path,
        fixture_command("basic"),
        overrides={"shell": {"enabled": True, "max_output_chars": 8}},
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("!echo hi", message_id=11)
        await service._handle_bang_shell(message, "!echo hi", runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("Output too long" in msg["text"] for msg in fake_bot.messages)
    assert any("echo" in msg["text"] for msg in fake_bot.messages)
    assert fake_bot.documents


@pytest.mark.anyio
async def test_bang_shell_timeout_message(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(
        tmp_path,
        fixture_command("command_exec_hang"),
        overrides={"shell": {"enabled": True, "timeout_ms": 100}},
    )
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("!top", message_id=11)
        await service._handle_bang_shell(message, "!top", runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("timed out" in msg["text"] for msg in fake_bot.messages)
    assert any("top -l 1" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_diff_command_uses_app_server(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("/diff", message_id=11)
        await service._handle_diff(message, "", runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("fixture output" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_thread_start_rejects_missing_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_start_missing_cwd"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("did not return a workspace" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_thread_start_rejects_mismatched_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_start_mismatch"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    try:
        await service._handle_bind(bind_message, str(repo))
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        runtime = service._router.runtime_for(key)
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any(
        "returned a thread for a different workspace" in msg["text"]
        for msg in fake_bot.messages
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing_resume_scenario",
    ["thread_resume_missing_thread", "thread_resume_missing_rollout"],
)
async def test_stale_active_thread_is_recovered_during_verification(
    tmp_path: Path,
    missing_resume_scenario: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command(missing_resume_scenario))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    prompt_message = build_message("hello", message_id=12)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
        runtime = service._router.runtime_for(
            await service._router.resolve_key(
                bind_message.chat_id, bind_message.thread_id
            )
        )
        await service._handle_normal_message(prompt_message, runtime)
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        record = await service._router.get_topic(key)
    finally:
        await service._app_server_supervisor.close_all()
    assert record is not None
    assert record.active_thread_id == "thread-2"
    assert not any(
        "Failed to verify the active thread; use /resume or /new." in msg["text"]
        for msg in fake_bot.messages
    )
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_new_surfaces_thread_start_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_start_error"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
    finally:
        await service._app_server_supervisor.close_all()
    assert any(
        "Failed to start a new thread; check logs for details." in msg["text"]
        for msg in fake_bot.messages
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing_resume_scenario",
    ["thread_resume_missing_thread", "thread_resume_missing_rollout"],
)
async def test_resume_missing_thread_clears_stale_topic_state(
    tmp_path: Path,
    missing_resume_scenario: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command(missing_resume_scenario))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
        key = await service._router.resolve_key(
            bind_message.chat_id, bind_message.thread_id
        )
        await service._resume_thread_by_id(key, "thread-1")
        record = await service._router.get_topic(key)
    finally:
        await service._app_server_supervisor.close_all()
    assert record is not None
    assert record.active_thread_id is None
    assert "thread-1" not in record.thread_ids
    assert any("Thread no longer exists." in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_resume_lists_threads_from_data_shape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_data_shape"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    resume_message = build_message("/resume", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_resume(resume_message, "--all")
    finally:
        await service._app_server_supervisor.close_all()
    assert any("Select a thread to resume" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_resume_all_uses_local_workspace_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_empty"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    bind_message_other = build_message("/bind", thread_id=99, message_id=11)
    new_message = build_message("/new", message_id=12)
    new_message_other = build_message("/new", thread_id=99, message_id=13)
    resume_message = build_message("/resume", message_id=14)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_bind(bind_message_other, str(repo))
        await service._handle_new(new_message)
        await service._handle_new(new_message_other)
        await service._handle_resume(resume_message, "--all")
    finally:
        await service._app_server_supervisor.close_all()
    resume_msg = next(
        msg for msg in fake_bot.messages if "Select a thread to resume" in msg["text"]
    )
    keyboard = resume_msg["reply_markup"]["inline_keyboard"]
    callback_data = [
        button["callback_data"]
        for row in keyboard
        for button in row
        if "callback_data" in button
    ]
    assert any("thread-1" in token for token in callback_data)
    assert any("thread-2" in token for token in callback_data)


@pytest.mark.anyio
async def test_resume_requires_scoped_threads(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    resume_message = build_message("/resume", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_resume(resume_message, "")
    finally:
        await service._app_server_supervisor.close_all()
    assert any(
        "No previous threads found for this topic" in msg["text"]
        for msg in fake_bot.messages
    )


@pytest.mark.anyio
async def test_resume_shows_local_threads_when_thread_list_empty(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_empty"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    resume_message = build_message("/resume", message_id=12)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
        await service._handle_resume(resume_message, "")
    finally:
        await service._app_server_supervisor.close_all()
    assert not any(
        "No previous threads found" in msg["text"] for msg in fake_bot.messages
    )
    resume_msg = next(
        msg for msg in fake_bot.messages if "Select a thread to resume" in msg["text"]
    )
    keyboard = resume_msg["reply_markup"]["inline_keyboard"]
    assert any(
        "thread-1" in button["callback_data"]
        for row in keyboard
        for button in row
        if "callback_data" in button
    )


@pytest.mark.anyio
async def test_resume_refresh_updates_cached_preview(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_empty_refresh"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message = build_message("/new", message_id=11)
    resume_message = build_message("/resume", message_id=12)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message)
        await service._handle_resume(resume_message, "--refresh")
    finally:
        await service._app_server_supervisor.close_all()
    resume_msg = next(
        msg for msg in fake_bot.messages if "Select a thread to resume" in msg["text"]
    )
    keyboard = resume_msg["reply_markup"]["inline_keyboard"]
    labels = [button["text"] for row in keyboard for button in row if "text" in button]
    assert any("refreshed preview" in label for label in labels)


@pytest.mark.anyio
async def test_resume_compact_seed_button_label_is_condensed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_compact_seed"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    resume_message = build_message("/resume", message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_resume(resume_message, "--all")
    finally:
        await service._app_server_supervisor.close_all()
    resume_msg = next(
        msg for msg in fake_bot.messages if "Select a thread to resume" in msg["text"]
    )
    keyboard = resume_msg["reply_markup"]["inline_keyboard"]
    labels = [button["text"] for row in keyboard for button in row if "text" in button]
    assert any("Compacted:" in label for label in labels)
    assert all("Context from previous conversation" not in label for label in labels)
    assert any("2026-03-31 09:15Z" in label for label in labels)


@pytest.mark.anyio
async def test_resume_paginates_thread_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("thread_list_paged"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)
    new_message_1 = build_message("/new", message_id=11)
    new_message_2 = build_message("/new", message_id=12)
    new_message_3 = build_message("/new", message_id=13)
    resume_message = build_message("/resume", message_id=14)
    try:
        await service._handle_bind(bind_message, str(repo))
        await service._handle_new(new_message_1)
        await service._handle_new(new_message_2)
        await service._handle_new(new_message_3)
        await service._handle_resume(resume_message, "")
    finally:
        await service._app_server_supervisor.close_all()
    resume_msg = next(
        msg for msg in fake_bot.messages if "Select a thread to resume" in msg["text"]
    )
    keyboard = resume_msg["reply_markup"]["inline_keyboard"]
    callback_data = [
        button["callback_data"]
        for row in keyboard
        for button in row
        if "callback_data" in button
    ]
    assert any("thread-1" in token for token in callback_data)
    assert any("thread-2" in token for token in callback_data)
    assert any("thread-3" in token for token in callback_data)


@pytest.mark.anyio
async def test_update_with_all_target_prompts_for_confirmation_when_turn_active(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    message = build_message("/update all", message_id=20)
    captured: dict[str, object] = {}

    async def _fake_prompt_update_confirmation(
        msg: TelegramMessage, *, update_target: Optional[str] = None
    ) -> None:
        captured["message_id"] = msg.message_id
        captured["update_target"] = update_target

    service._turn_contexts[("thread-1", "turn-1")] = object()  # type: ignore[assignment]
    service._prompt_update_confirmation = _fake_prompt_update_confirmation  # type: ignore[assignment]

    try:
        await service._handle_update(message, "all", None)
    finally:
        await service._app_server_supervisor.close_all()

    assert captured == {"message_id": 20, "update_target": "all"}


@pytest.mark.anyio
async def test_update_web_target_skips_confirmation_when_turn_active(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    message = build_message("/update web", message_id=21)
    captured: dict[str, object] = {}

    async def _fake_prompt_update_confirmation(
        _msg: TelegramMessage, *, update_target: Optional[str] = None
    ) -> None:
        captured["prompted"] = update_target

    async def _fake_start_update(
        *,
        chat_id: int,
        thread_id: Optional[int],
        update_target: str,
        reply_to: Optional[int] = None,
        callback=None,
        selection_key: Optional[str] = None,
    ) -> None:
        captured["chat_id"] = chat_id
        captured["thread_id"] = thread_id
        captured["update_target"] = update_target
        captured["reply_to"] = reply_to

    service._turn_contexts[("thread-1", "turn-1")] = object()  # type: ignore[assignment]
    service._prompt_update_confirmation = _fake_prompt_update_confirmation  # type: ignore[assignment]
    service._start_update = _fake_start_update  # type: ignore[assignment]

    try:
        await service._handle_update(message, "web", None)
    finally:
        await service._app_server_supervisor.close_all()

    assert captured == {
        "chat_id": 123,
        "thread_id": None,
        "update_target": "web",
        "reply_to": 21,
    }


@pytest.mark.anyio
async def test_update_callback_acknowledged_before_spawn(tmp_path: Path) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    callback = TelegramCallbackQuery(
        update_id=1,
        callback_id="cb-1",
        from_user_id=456,
        data="update:discord",
        message_id=22,
        chat_id=123,
        thread_id=None,
    )
    events: list[tuple[str, str]] = []

    async def _fake_answer_callback(
        cb: Optional[TelegramCallbackQuery], text: str
    ) -> None:
        assert cb == callback
        events.append(("answer", text))

    async def _fake_finalize_selection(
        key: str,
        cb: Optional[TelegramCallbackQuery],
        text: str,
    ) -> None:
        assert key == "topic-key"
        assert cb == callback
        events.append(("finalize", text))

    def _fake_spawn_update_process(**kwargs: object) -> None:
        assert kwargs["update_target"] == "discord"
        events.append(("spawn", "discord"))

    service._answer_callback = _fake_answer_callback  # type: ignore[assignment]
    service._finalize_selection = _fake_finalize_selection  # type: ignore[assignment]

    original_spawn = telegram_commands_runtime._spawn_update_process
    telegram_commands_runtime._spawn_update_process = _fake_spawn_update_process  # type: ignore[assignment]

    try:
        await service._start_update(
            chat_id=123,
            thread_id=None,
            update_target="discord",
            callback=callback,
            selection_key="topic-key",
        )
    finally:
        telegram_commands_runtime._spawn_update_process = original_spawn  # type: ignore[assignment]
        await service._app_server_supervisor.close_all()

    assert events == [
        ("answer", "Starting update..."),
        ("spawn", "discord"),
        (
            "finalize",
            "Update started (discord). The selected service(s) will restart.",
        ),
    ]


@pytest.mark.anyio
async def test_outbox_lock_rebinds_across_event_loops(tmp_path: Path) -> None:
    config = make_config(tmp_path, fixture_command("basic"))
    service = build_service_in_closed_loop(tmp_path, config)
    try:
        assert await service._mark_outbox_inflight("record")
        assert "record" in service._outbox_inflight
        await service._clear_outbox_inflight("record")
        assert "record" not in service._outbox_inflight
    finally:
        await service._app_server_supervisor.close_all()
