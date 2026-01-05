import types

import pytest

from codex_autorunner.telegram_adapter import (
    ApprovalCallback,
    BindCallback,
    CancelCallback,
    UpdateCallback,
    PageCallback,
    ResumeCallback,
    TelegramAllowlist,
    TelegramUpdate,
    TelegramMessage,
    TelegramCommand,
    TelegramMessageEntity,
    allowlist_allows,
    build_approval_keyboard,
    build_bind_keyboard,
    build_resume_keyboard,
    build_update_keyboard,
    chunk_message,
    encode_approval_callback,
    encode_bind_callback,
    encode_cancel_callback,
    encode_page_callback,
    encode_resume_callback,
    encode_update_callback,
    is_interrupt_alias,
    next_update_offset,
    parse_callback_data,
    parse_command,
    parse_update,
    TelegramBotClient,
    TELEGRAM_MAX_MESSAGE_LENGTH,
)


def test_parse_command_basic() -> None:
    entities = [TelegramMessageEntity(type="bot_command", offset=0, length=len("/new"))]
    command = parse_command("/new", entities=entities)
    assert command == TelegramCommand(name="new", args="", raw="/new")


def test_parse_command_with_args() -> None:
    entities = [
        TelegramMessageEntity(type="bot_command", offset=0, length=len("/bind"))
    ]
    command = parse_command("/bind repo-1", entities=entities)
    assert command == TelegramCommand(name="bind", args="repo-1", raw="/bind repo-1")


def test_parse_command_username_match() -> None:
    token = "/resume@CodexBot"
    entities = [TelegramMessageEntity(type="bot_command", offset=0, length=len(token))]
    command = parse_command(
        f"{token} 3",
        entities=entities,
        bot_username="CodexBot",
    )
    assert command == TelegramCommand(name="resume", args="3", raw="/resume@CodexBot 3")


def test_parse_command_username_mismatch() -> None:
    token = "/resume@OtherBot"
    entities = [TelegramMessageEntity(type="bot_command", offset=0, length=len(token))]
    command = parse_command(
        f"{token} 3",
        entities=entities,
        bot_username="CodexBot",
    )
    assert command is None


def test_parse_command_requires_entity() -> None:
    command = parse_command("/mnt/data/file.txt")
    assert command is None


def test_parse_command_requires_offset_zero() -> None:
    entities = [TelegramMessageEntity(type="bot_command", offset=1, length=len("/new"))]
    command = parse_command(" /new", entities=entities)
    assert command is None


def test_is_interrupt_aliases() -> None:
    for text in ("^C", "^c", "ctrl-c", "CTRL+C", "esc", "Escape", "/interrupt"):
        assert is_interrupt_alias(text)


def test_allowlist_allows_message() -> None:
    update = TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=123,
            thread_id=99,
            from_user_id=456,
            text="hello",
            date=0,
            is_topic_message=True,
        ),
        callback=None,
    )
    allowlist = TelegramAllowlist({123}, {456}, require_topic=True)
    assert allowlist_allows(update, allowlist)


def test_allowlist_blocks_missing_topic() -> None:
    update = TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=123,
            thread_id=None,
            from_user_id=456,
            text="hello",
            date=0,
            is_topic_message=False,
        ),
        callback=None,
    )
    allowlist = TelegramAllowlist({123}, {456}, require_topic=True)
    assert not allowlist_allows(update, allowlist)


def test_allowlist_blocks_missing_lists() -> None:
    update = TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            update_id=1,
            message_id=2,
            chat_id=123,
            thread_id=None,
            from_user_id=456,
            text="hello",
            date=0,
            is_topic_message=False,
        ),
        callback=None,
    )
    allowlist = TelegramAllowlist(set(), set())
    assert not allowlist_allows(update, allowlist)


def test_parse_update_message() -> None:
    update = {
        "update_id": 9,
        "message": {
            "message_id": 2,
            "chat": {"id": -123},
            "message_thread_id": 77,
            "from": {"id": 456},
            "text": "hi",
            "date": 1,
            "is_topic_message": True,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.chat_id == -123
    assert parsed.message.thread_id == 77
    assert parsed.message.text == "hi"
    assert parsed.message.is_edited is False
    assert parsed.callback is None


def test_parse_update_callback() -> None:
    update = {
        "update_id": 10,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 456},
            "data": "resume:thread_1",
            "message": {"message_id": 7, "chat": {"id": 123}, "message_thread_id": 88},
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.callback is not None
    assert parsed.callback.chat_id == 123
    assert parsed.callback.thread_id == 88
    assert parsed.message is None


def test_parse_update_photo_caption() -> None:
    update = {
        "update_id": 11,
        "message": {
            "message_id": 3,
            "chat": {"id": 456},
            "from": {"id": 999},
            "photo": [
                {
                    "file_id": "photo-small",
                    "file_unique_id": "unique-small",
                    "width": 64,
                    "height": 64,
                    "file_size": 1200,
                },
                {
                    "file_id": "photo-large",
                    "file_unique_id": "unique-large",
                    "width": 1024,
                    "height": 768,
                    "file_size": 90000,
                },
            ],
            "caption": "Check this",
            "date": 1,
            "is_topic_message": False,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.caption == "Check this"
    assert len(parsed.message.photos) == 2
    assert parsed.message.photos[0].file_id == "photo-small"


def test_parse_update_edited_message() -> None:
    update = {
        "update_id": 15,
        "edited_message": {
            "message_id": 6,
            "chat": {"id": 321},
            "from": {"id": 999},
            "text": "edited",
            "date": 1,
            "edit_date": 2,
            "is_topic_message": False,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.is_edited is True


def test_parse_update_voice() -> None:
    update = {
        "update_id": 12,
        "message": {
            "message_id": 4,
            "chat": {"id": 456},
            "from": {"id": 999},
            "voice": {
                "file_id": "voice-1",
                "file_unique_id": "voice-unique",
                "duration": 6,
                "mime_type": "audio/ogg",
                "file_size": 2048,
            },
            "date": 1,
            "is_topic_message": False,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.voice is not None
    assert parsed.message.voice.file_id == "voice-1"


def test_chunk_message_with_numbering() -> None:
    text = "alpha " * 200
    parts = chunk_message(text, max_len=120, with_numbering=True)
    assert len(parts) > 1
    assert parts[0].startswith("Part 1/")
    assert parts[-1].startswith(f"Part {len(parts)}/")


def test_chunk_message_no_numbering() -> None:
    text = "alpha " * 200
    parts = chunk_message(text, max_len=120, with_numbering=False)
    assert len(parts) > 1
    assert not parts[0].startswith("Part 1/")


def test_chunk_message_empty() -> None:
    assert chunk_message("") == []
    assert chunk_message(None) == []


@pytest.mark.anyio
async def test_send_message_chunks_long_text() -> None:
    client = TelegramBotClient("test-token")
    calls: list[dict[str, object]] = []

    async def fake_request(self, method: str, payload: dict[str, object]) -> object:
        calls.append({"method": method, "payload": payload})
        return {"message_id": len(calls)}

    client._request = types.MethodType(fake_request, client)
    try:
        text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 5)
        response = await client.send_message(
            123,
            text,
            reply_markup={"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]},
            parse_mode="Markdown",
        )
    finally:
        await client.close()

    assert response.get("message_id") == 1
    assert len(calls) == 2
    first_payload = calls[0]["payload"]
    second_payload = calls[1]["payload"]
    assert "reply_markup" in first_payload
    assert "reply_markup" not in second_payload


def test_callback_encoding_and_parsing() -> None:
    approval = encode_approval_callback("accept", "req1")
    parsed = parse_callback_data(approval)
    assert parsed == ApprovalCallback(decision="accept", request_id="req1")
    resume = encode_resume_callback("thread_1")
    parsed_resume = parse_callback_data(resume)
    assert parsed_resume == ResumeCallback(thread_id="thread_1")
    bind = encode_bind_callback("repo_1")
    parsed_bind = parse_callback_data(bind)
    assert parsed_bind == BindCallback(repo_id="repo_1")
    update = encode_update_callback("web")
    parsed_update = parse_callback_data(update)
    assert parsed_update == UpdateCallback(target="web")
    cancel = encode_cancel_callback("resume")
    parsed_cancel = parse_callback_data(cancel)
    assert parsed_cancel == CancelCallback(kind="resume")
    page = encode_page_callback("resume", 2)
    parsed_page = parse_callback_data(page)
    assert parsed_page == PageCallback(kind="resume", page=2)


def test_build_keyboards() -> None:
    keyboard = build_approval_keyboard("req1", include_session=True)
    assert keyboard["inline_keyboard"][0][0]["text"] == "Accept"
    resume_keyboard = build_resume_keyboard([("thread_a", "1) foo")])
    assert resume_keyboard["inline_keyboard"][0][0]["callback_data"].startswith(
        "resume:"
    )
    resume_paged = build_resume_keyboard(
        [("thread_a", "1) foo")],
        page_button=("More...", encode_page_callback("resume", 1)),
        include_cancel=True,
    )
    assert resume_paged["inline_keyboard"][1][0]["text"] == "More..."
    assert resume_paged["inline_keyboard"][2][0]["callback_data"].startswith("cancel:")
    bind_keyboard = build_bind_keyboard([("repo_a", "1) repo-a")])
    assert bind_keyboard["inline_keyboard"][0][0]["callback_data"].startswith("bind:")
    update_keyboard = build_update_keyboard(
        [("both", "Both"), ("web", "Web only")],
        include_cancel=True,
    )
    assert update_keyboard["inline_keyboard"][0][0]["callback_data"].startswith(
        "update:"
    )
    assert update_keyboard["inline_keyboard"][-1][0]["callback_data"].startswith(
        "cancel:"
    )


def test_next_update_offset() -> None:
    updates = [{"update_id": 1}, {"update_id": 3}, {"update_id": 2}]
    assert next_update_offset(updates, None) == 4
    assert next_update_offset([], 5) == 5
