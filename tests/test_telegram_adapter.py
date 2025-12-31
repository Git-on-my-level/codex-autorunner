from codex_autorunner.telegram_adapter import (
    ApprovalCallback,
    BindCallback,
    ResumeCallback,
    TelegramAllowlist,
    TelegramUpdate,
    TelegramMessage,
    TelegramCommand,
    allowlist_allows,
    build_approval_keyboard,
    build_bind_keyboard,
    build_resume_keyboard,
    chunk_message,
    encode_approval_callback,
    encode_bind_callback,
    encode_resume_callback,
    is_interrupt_alias,
    next_update_offset,
    parse_callback_data,
    parse_command,
    parse_update,
)


def test_parse_command_basic() -> None:
    command = parse_command("/new")
    assert command == TelegramCommand(name="new", args="", raw="/new")


def test_parse_command_with_args() -> None:
    command = parse_command("/bind repo-1")
    assert command == TelegramCommand(name="bind", args="repo-1", raw="/bind repo-1")


def test_parse_command_username_match() -> None:
    command = parse_command("/resume@CodexBot 3", bot_username="CodexBot")
    assert command == TelegramCommand(name="resume", args="3", raw="/resume@CodexBot 3")


def test_parse_command_username_mismatch() -> None:
    command = parse_command("/resume@OtherBot 3", bot_username="CodexBot")
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


def test_build_keyboards() -> None:
    keyboard = build_approval_keyboard("req1", include_session=True)
    assert keyboard["inline_keyboard"][0][0]["text"] == "Accept"
    resume_keyboard = build_resume_keyboard([("thread_a", "1) foo")])
    assert resume_keyboard["inline_keyboard"][0][0]["callback_data"].startswith(
        "resume:"
    )
    bind_keyboard = build_bind_keyboard([("repo_a", "1) repo-a")])
    assert bind_keyboard["inline_keyboard"][0][0]["callback_data"].startswith("bind:")


def test_next_update_offset() -> None:
    updates = [{"update_id": 1}, {"update_id": 3}, {"update_id": 2}]
    assert next_update_offset(updates, None) == 4
    assert next_update_offset([], 5) == 5
