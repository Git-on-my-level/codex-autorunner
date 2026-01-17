import asyncio
import sys
from pathlib import Path
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
)
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "app_server_fixture.py"

pytestmark = pytest.mark.integration


def fixture_command(scenario: str) -> list[str]:
    return [sys.executable, "-u", str(FIXTURE_PATH), "--scenario", scenario]


def make_config(
    root: Path, command: list[str], overrides: Optional[dict[str, object]] = None
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
    return TelegramBotConfig.from_raw(raw, root=root, env=env)


def build_message(
    text: str,
    *,
    chat_id: int = 123,
    thread_id: Optional[int] = None,
    user_id: int = 456,
    message_id: int = 1,
    update_id: int = 1,
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
    record = service._router.get_topic(
        service._router.resolve_key(message.chat_id, message.thread_id)
    )
    assert record is not None


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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any("Bound to" in msg["text"] for msg in fake_bot.messages)
    assert any("fixture reply" in msg["text"] for msg in fake_bot.messages)


@pytest.mark.anyio
async def test_document_message_saves_inbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = make_config(tmp_path, fixture_command("basic"))
    service = TelegramBotService(config, hub_root=tmp_path)
    fake_bot = FakeBot()
    service._bot = fake_bot
    bind_message = build_message("/bind", message_id=10)

    async def fake_download(_file_id: str) -> tuple[bytes, str, int]:
        return b"data", "files/report.txt", 4

    service._download_telegram_file = fake_download
    document = TelegramDocument("d1", None, "report.txt", "text/plain", 4)
    message = build_document_message(document, message_id=11)
    try:
        await service._handle_bind(bind_message, str(repo))
        runtime = service._router.runtime_for(
            service._router.resolve_key(message.chat_id, message.thread_id)
        )
        await service._handle_media_message(message, runtime, "")
    finally:
        await service._app_server_supervisor.close_all()
    inbox_root = repo / ".codex-autorunner" / "uploads" / "telegram-files"
    inbox_files = [path for path in inbox_root.rglob("*") if path.is_file()]
    assert inbox_files


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
        key = service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
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
        runtime = service._router.runtime_for(
            service._router.resolve_key(bind_message.chat_id, bind_message.thread_id)
        )
        message = build_message("hello", message_id=11)
        await service._handle_normal_message(message, runtime)
    finally:
        await service._app_server_supervisor.close_all()
    assert any(
        "returned a thread for a different workspace" in msg["text"]
        for msg in fake_bot.messages
    )


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
    assert all("Context handoff from previous thread" not in label for label in labels)


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
