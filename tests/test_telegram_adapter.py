import asyncio
import types

import httpx
import pytest

from codex_autorunner.core.exceptions import CircuitOpenError
from codex_autorunner.integrations.telegram.adapter import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    ApprovalCallback,
    BindCallback,
    CancelCallback,
    CompactCallback,
    FlowCallback,
    FlowRunCallback,
    PageCallback,
    QuestionCancelCallback,
    QuestionCustomCallback,
    QuestionDoneCallback,
    QuestionOptionCallback,
    ResumeCallback,
    ReviewCommitCallback,
    TelegramAllowlist,
    TelegramAPIError,
    TelegramBotClient,
    TelegramCommand,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramUpdate,
    UpdateCallback,
    allowlist_allows,
    build_approval_keyboard,
    build_bind_keyboard,
    build_flow_runs_keyboard,
    build_question_keyboard,
    build_resume_keyboard,
    build_review_commit_keyboard,
    build_update_keyboard,
    chunk_message,
    encode_approval_callback,
    encode_bind_callback,
    encode_cancel_callback,
    encode_compact_callback,
    encode_flow_callback,
    encode_flow_run_callback,
    encode_page_callback,
    encode_question_cancel_callback,
    encode_question_custom_callback,
    encode_question_done_callback,
    encode_question_option_callback,
    encode_resume_callback,
    encode_review_commit_callback,
    encode_update_callback,
    is_interrupt_alias,
    next_update_offset,
    parse_callback_data,
    parse_command,
    parse_update,
)
from codex_autorunner.integrations.telegram.api_schemas import (
    TelegramCallbackQuerySchema,
    TelegramDocumentSchema,
    TelegramMessageEntitySchema,
    TelegramMessageSchema,
    TelegramPhotoSizeSchema,
    TelegramUpdateSchema,
    TelegramVoiceSchema,
    parse_callback_query_payload,
    parse_message_payload,
    parse_update_payload,
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


def test_parse_command_fallback_basic() -> None:
    command = parse_command("/review")
    assert command == TelegramCommand(name="review", args="", raw="/review")


def test_parse_command_fallback_with_args() -> None:
    command = parse_command("/review pr")
    assert command == TelegramCommand(name="review", args="pr", raw="/review pr")


def test_parse_command_fallback_rejects_path() -> None:
    command = parse_command("/mnt/data/file.txt")
    assert command is None


def test_parse_command_fallback_username_match() -> None:
    command = parse_command("/review@CodexBot", bot_username="CodexBot")
    assert command == TelegramCommand(name="review", args="", raw="/review@CodexBot")


def test_parse_command_fallback_username_mismatch() -> None:
    command = parse_command("/review@OtherBot", bot_username="CodexBot")
    assert command is None


def test_parse_command_fallback_whitespace() -> None:
    command = parse_command("  /review  pr  ")
    assert command == TelegramCommand(name="review", args="pr", raw="/review  pr")


def test_parse_command_fallback_rejects_hyphen() -> None:
    command = parse_command("/foo-bar")
    assert command is None


def test_parse_command_fallback_rejects_question() -> None:
    command = parse_command("/foo?")
    assert command is None


def test_parse_command_fallback_rejects_exclamation() -> None:
    command = parse_command("/foo!")
    assert command is None


def test_parse_command_fallback_rejects_uppercase() -> None:
    command = parse_command("/Review")
    assert command is None


def test_parse_command_fallback_rejects_too_long() -> None:
    command = parse_command("/" + "a" * 33)
    assert command is None


def test_parse_command_requires_offset_zero() -> None:
    entities = [TelegramMessageEntity(type="bot_command", offset=1, length=len("/new"))]
    command = parse_command(" /new", entities=entities)
    assert command is None


def test_is_interrupt_aliases() -> None:
    for text in (
        "^C",
        "^c",
        "ctrl-c",
        "CTRL+C",
        "esc",
        "Escape",
        "/interrupt",
        "/stop",
    ):
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


def test_parse_question_option_callback() -> None:
    data = encode_question_option_callback("req:1", 2, 3)
    parsed = parse_callback_data(data)
    assert parsed == QuestionOptionCallback(
        request_id="req:1",
        question_index=2,
        option_index=3,
    )


def test_parse_question_cancel_callback() -> None:
    data = encode_question_cancel_callback("req:1")
    parsed = parse_callback_data(data)
    assert parsed == QuestionCancelCallback(request_id="req:1")


def test_encode_parse_question_done_callback() -> None:
    data = encode_question_done_callback("req:1")
    parsed = parse_callback_data(data)
    assert parsed == QuestionDoneCallback(request_id="req:1")


def test_encode_parse_question_custom_callback() -> None:
    data = encode_question_custom_callback("req:1")
    parsed = parse_callback_data(data)
    assert parsed == QuestionCustomCallback(request_id="req:1")


def test_build_question_keyboard() -> None:
    keyboard = build_question_keyboard("req:1", question_index=0, options=["Yes"])
    assert keyboard["inline_keyboard"][0][0]["callback_data"].startswith("qopt:")


def test_build_question_keyboard_multi_select() -> None:
    keyboard = build_question_keyboard(
        "req:1",
        question_index=0,
        options=["Yes", "No", "Maybe"],
        multiple=True,
    )
    rows = keyboard["inline_keyboard"]
    assert len(rows) == 6
    assert "Done" in rows[4][0]["text"]


def test_build_question_keyboard_with_custom() -> None:
    keyboard = build_question_keyboard(
        "req:1",
        question_index=0,
        options=["Yes", "No"],
        custom=True,
    )
    rows = keyboard["inline_keyboard"]
    assert len(rows) == 4
    assert "Type your own answer" in rows[2][0]["text"]


def test_build_question_keyboard_selected_indices() -> None:
    keyboard = build_question_keyboard(
        "req:1",
        question_index=0,
        options=["A", "B", "C"],
        multiple=True,
        selected_indices={0, 2},
    )
    rows = keyboard["inline_keyboard"]
    assert rows[0][0]["text"] == "✓ A"
    assert rows[1][0]["text"] == "B"
    assert rows[2][0]["text"] == "✓ C"


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
    assert parsed.message.media_group_id is None


def test_parse_update_media_group_id() -> None:
    update = {
        "update_id": 12,
        "message": {
            "message_id": 4,
            "chat": {"id": 456},
            "from": {"id": 999},
            "photo": [
                {
                    "file_id": "photo-1",
                    "file_unique_id": "unique-1",
                    "width": 100,
                    "height": 100,
                }
            ],
            "caption": "First photo",
            "media_group_id": "abc123",
            "date": 1,
            "is_topic_message": False,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.media_group_id == "abc123"
    assert len(parsed.message.photos) == 1


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
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = TelegramBotClient("test-token", client=http_client)
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


@pytest.mark.anyio
async def test_request_retries_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests: retry after 1",
                    "parameters": {"retry_after": 1},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    try:
        response = await bot.send_message(123, "hello")
    finally:
        await bot.close()

    assert response.get("message_id") == 123
    assert calls["count"] == 2
    assert sleeps and sleeps[0] >= 0.9


@pytest.mark.anyio
async def test_rate_limit_scope_does_not_block_other_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}})
    )
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.adapter.asyncio.sleep",
        fake_sleep,
    )
    try:
        loop = asyncio.get_running_loop()
        bot._rate_limit_until["editMessageText"] = loop.time() + 60.0
        await bot._wait_for_rate_limit("getUpdates")
    finally:
        await bot.close()

    assert sleeps == []


@pytest.mark.anyio
async def test_circuit_breaker_scope_isolated_per_method() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "editMessageText":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    bot = TelegramBotClient("test-token", client=http_client)
    try:
        for _ in range(6):
            with pytest.raises((TelegramAPIError, CircuitOpenError)):
                await bot.edit_message_text(123, 1, "noop")
        response = await bot.send_message(123, "hello")
    finally:
        await bot.close()

    assert response.get("message_id") == 123


@pytest.mark.anyio
async def test_download_file_uses_file_base_url() -> None:
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = TelegramBotClient("test-token", client=http_client)
    try:
        payload = await client.download_file("photos/file_1.jpg")
    finally:
        await client.close()

    assert payload == b"ok"
    assert requested_urls == [
        "https://api.telegram.org/file/bottest-token/photos/file_1.jpg"
    ]


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
    review_commit = encode_review_commit_callback("abc123")
    parsed_review_commit = parse_callback_data(review_commit)
    assert parsed_review_commit == ReviewCommitCallback(sha="abc123")
    cancel = encode_cancel_callback("resume")
    parsed_cancel = parse_callback_data(cancel)
    assert parsed_cancel == CancelCallback(kind="resume")
    page = encode_page_callback("resume", 2)
    parsed_page = parse_callback_data(page)
    assert parsed_page == PageCallback(kind="resume", page=2)
    flow_run = encode_flow_run_callback("run-123")
    parsed_flow_run = parse_callback_data(flow_run)
    assert parsed_flow_run == FlowRunCallback(run_id="run-123")
    flow = encode_flow_callback("refresh", "run-123", repo_id="repo-1")
    parsed_flow = parse_callback_data(flow)
    assert parsed_flow == FlowCallback(
        action="refresh", run_id="run-123", repo_id="repo-1"
    )


def test_flow_callback_repo_id_requires_run_id() -> None:
    with pytest.raises(ValueError, match="requires run_id"):
        encode_flow_callback("refresh", repo_id="repo-1")


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
    review_keyboard = build_review_commit_keyboard([("abc123", "1) abc123")])
    assert review_keyboard["inline_keyboard"][0][0]["callback_data"].startswith(
        "review_commit:"
    )
    flow_runs_keyboard = build_flow_runs_keyboard([("run-1", "1) run-1")])
    assert flow_runs_keyboard["inline_keyboard"][0][0]["callback_data"].startswith(
        "flow_run:"
    )


def test_compact_callback_round_trip() -> None:
    data = encode_compact_callback("apply")
    parsed = parse_callback_data(data)
    assert parsed == CompactCallback(action="apply")


def test_compact_callback_invalid() -> None:
    assert parse_callback_data("compact:") is None


def test_next_update_offset() -> None:
    updates = [{"update_id": 1}, {"update_id": 3}, {"update_id": 2}]
    assert next_update_offset(updates, None) == 4
    assert next_update_offset([], 5) == 5


def test_api_schema_parse_update_basic() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 456},
            "text": "hello",
            "date": 1234567890,
        },
    }
    schema = parse_update_payload(update)
    assert isinstance(schema, TelegramUpdateSchema)
    assert schema.update_id == 1
    assert schema.message is not None
    assert schema.edited_message is None
    assert schema.callback_query is None


def test_api_schema_parse_update_with_callback() -> None:
    update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb123",
            "from": {"id": 456},
            "data": "test",
            "message": {"message_id": 7, "chat": {"id": 123}},
        },
    }
    schema = parse_update_payload(update)
    assert isinstance(schema, TelegramUpdateSchema)
    assert schema.update_id == 2
    assert schema.message is None
    assert schema.callback_query is not None


def test_api_schema_parse_update_tolerates_unknown_fields() -> None:
    update = {
        "update_id": 3,
        "message": {
            "message_id": 4,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 456},
            "text": "hello",
            "date": 1234567890,
            "unknown_field": "should be ignored",
            "another_unknown": {"nested": "data"},
        },
        "unknown_update_field": "ignored",
    }
    schema = parse_update_payload(update)
    assert isinstance(schema, TelegramUpdateSchema)
    assert schema.update_id == 3


def test_api_schema_parse_message_basic() -> None:
    message = {
        "message_id": 1,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "text": "hello",
        "date": 1234567890,
        "is_topic_message": False,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.message_id == 1
    assert schema.chat["id"] == 123
    assert schema.text == "hello"
    assert schema.caption is None


def test_api_schema_parse_message_with_caption_entities() -> None:
    message = {
        "message_id": 2,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "photo": [
            {
                "file_id": "photo_1",
                "file_unique_id": "unique_1",
                "width": 100,
                "height": 100,
            }
        ],
        "caption": "Look at this!",
        "caption_entities": [{"type": "bold", "offset": 0, "length": 4}],
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.caption == "Look at this!"
    assert schema.caption_entities is not None
    assert len(schema.caption_entities) == 1
    assert schema.caption_entities[0]["type"] == "bold"


def test_api_schema_parse_message_text_entities() -> None:
    message = {
        "message_id": 3,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "text": "/command arg1 arg2",
        "entities": [{"type": "bot_command", "offset": 0, "length": 8}],
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.text == "/command arg1 arg2"
    assert schema.entities is not None
    assert len(schema.entities) == 1
    assert schema.entities[0]["type"] == "bot_command"


def test_api_schema_parse_message_photo() -> None:
    message = {
        "message_id": 4,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "photo": [
            {
                "file_id": "small",
                "file_unique_id": "unique_small",
                "width": 64,
                "height": 64,
                "file_size": 1000,
            },
            {
                "file_id": "large",
                "file_unique_id": "unique_large",
                "width": 1024,
                "height": 768,
                "file_size": 50000,
            },
        ],
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.photo is not None
    assert len(schema.photo) == 2


def test_api_schema_parse_message_document() -> None:
    message = {
        "message_id": 5,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "document": {
            "file_id": "doc_123",
            "file_unique_id": "unique_doc",
            "file_name": "test.pdf",
            "mime_type": "application/pdf",
            "file_size": 102400,
        },
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.document is not None
    assert schema.document["file_name"] == "test.pdf"


def test_api_schema_parse_message_voice() -> None:
    message = {
        "message_id": 6,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 456},
        "voice": {
            "file_id": "voice_123",
            "file_unique_id": "unique_voice",
            "duration": 15,
            "mime_type": "audio/ogg",
            "file_size": 2048,
        },
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.voice is not None
    assert schema.voice["duration"] == 15


def test_api_schema_parse_callback_query() -> None:
    callback = {
        "id": "cb123",
        "from": {"id": 456, "is_bot": False, "username": "testuser"},
        "data": "action:payload",
        "message": {
            "message_id": 10,
            "chat": {"id": 123, "type": "private"},
            "message_thread_id": 5,
        },
    }
    schema = parse_callback_query_payload(callback)
    assert isinstance(schema, TelegramCallbackQuerySchema)
    assert schema.id == "cb123"
    assert schema.data == "action:payload"
    assert schema.message is not None


def test_api_schema_parse_callback_query_no_message() -> None:
    callback = {
        "id": "cb456",
        "from": {"id": 789},
        "data": "inline:data",
    }
    schema = parse_callback_query_payload(callback)
    assert isinstance(schema, TelegramCallbackQuerySchema)
    assert schema.id == "cb456"
    assert schema.message is None


def test_api_schema_parse_photo_size() -> None:
    photo_data = {
        "file_id": "photo_abc",
        "file_unique_id": "unique_abc",
        "width": 800,
        "height": 600,
        "file_size": 30000,
    }
    schema = TelegramPhotoSizeSchema.model_validate(photo_data)
    assert schema.file_id == "photo_abc"
    assert schema.width == 800
    assert schema.height == 600
    assert schema.file_size == 30000


def test_api_schema_parse_document() -> None:
    doc_data = {
        "file_id": "doc_xyz",
        "file_unique_id": "unique_xyz",
        "file_name": "report.pdf",
        "mime_type": "application/pdf",
        "file_size": 1024000,
    }
    schema = TelegramDocumentSchema.model_validate(doc_data)
    assert schema.file_id == "doc_xyz"
    assert schema.file_name == "report.pdf"
    assert schema.mime_type == "application/pdf"


def test_api_schema_parse_voice() -> None:
    voice_data = {
        "file_id": "voice_qwe",
        "file_unique_id": "unique_qwe",
        "duration": 30,
        "mime_type": "audio/ogg",
        "file_size": 4096,
    }
    schema = TelegramVoiceSchema.model_validate(voice_data)
    assert schema.file_id == "voice_qwe"
    assert schema.duration == 30
    assert schema.mime_type == "audio/ogg"


def test_api_schema_parse_message_entity() -> None:
    entity_data = {"type": "url", "offset": 10, "length": 20}
    schema = TelegramMessageEntitySchema.model_validate(entity_data)
    assert schema.type == "url"
    assert schema.offset == 10
    assert schema.length == 20


def test_api_schema_unknown_fields_tolerated() -> None:
    update = {
        "update_id": 99,
        "message": {
            "message_id": 100,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 456, "unknown_user_field": "ignored"},
            "text": "test",
            "date": 1234567890,
            "unknown_message_field": {"nested": "data"},
        },
        "unknown_update_field": 123,
    }
    schema = parse_update_payload(update)
    assert isinstance(schema, TelegramUpdateSchema)
    assert schema.update_id == 99


def test_api_schema_optional_fields() -> None:
    message = {
        "message_id": 7,
        "chat": {"id": 123, "type": "private"},
        "text": "minimal message",
        "date": 1234567890,
    }
    schema = parse_message_payload(message)
    assert isinstance(schema, TelegramMessageSchema)
    assert schema.message_id == 7
    assert schema.from_user is None
    assert schema.caption is None
    assert schema.entities is None


def test_api_schema_invalid_payload_returns_none() -> None:
    assert parse_message_payload(None) is None
    assert parse_message_payload("invalid") is None
    assert parse_message_payload({"message_id": "not an int"}) is None
    assert parse_callback_query_payload(None) is None
    assert parse_callback_query_payload("invalid") is None


def test_adapter_integration_with_schemas() -> None:
    update = {
        "update_id": 100,
        "message": {
            "message_id": 200,
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 888, "is_bot": False, "username": "test"},
            "text": "/test command",
            "date": 1234567890,
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.text == "/test command"
    assert parsed.message.chat_id == 999
    assert parsed.message.from_user_id == 888
    assert len(parsed.message.entities) == 1
    assert parsed.message.entities[0].type == "bot_command"


def test_golden_fixture_photo_with_caption() -> None:
    update = {
        "update_id": 300,
        "message": {
            "message_id": 400,
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "from": {"id": 123456789, "is_bot": False, "username": "user"},
            "photo": [
                {
                    "file_id": "AgACAgIAAxkBAAIe...",
                    "file_unique_id": "AQAD...",
                    "width": 90,
                    "height": 90,
                    "file_size": 1234,
                },
                {
                    "file_id": "AgACAgIAAxkBAAIe...BQ",
                    "file_unique_id": "AQAD...BQ",
                    "width": 320,
                    "height": 320,
                    "file_size": 5678,
                },
                {
                    "file_id": "AgACAgIAAxkBAAIe...BA",
                    "file_unique_id": "AQAD...BA",
                    "width": 800,
                    "height": 800,
                    "file_size": 12345,
                },
            ],
            "caption": "Check out this photo! #amazing",
            "caption_entities": [{"type": "hashtag", "offset": 21, "length": 8}],
            "date": 1234567890,
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.message is not None
    assert parsed.message.caption == "Check out this photo! #amazing"
    assert len(parsed.message.photos) == 3
    assert parsed.message.photos[0].width == 90
    assert parsed.message.photos[2].width == 800
    assert len(parsed.message.caption_entities) == 1
    assert parsed.message.caption_entities[0].type == "hashtag"


def test_golden_fixture_complex_callback() -> None:
    update = {
        "update_id": 400,
        "callback_query": {
            "id": "438129494",
            "from": {
                "id": 123456789,
                "is_bot": False,
                "first_name": "John",
                "last_name": "Doe",
                "username": "johndoe",
            },
            "message": {
                "message_id": 500,
                "chat": {"id": -1001234567890, "type": "supergroup"},
                "message_thread_id": 42,
                "date": 1234567890,
            },
            "data": "appr:accept:req123",
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.callback is not None
    assert parsed.callback.callback_id == "438129494"
    assert parsed.callback.data == "appr:accept:req123"
    assert parsed.callback.chat_id == -1001234567890
    assert parsed.callback.thread_id == 42
