import asyncio
import types
from typing import Optional

import pytest

import codex_autorunner.integrations.telegram.handlers.messages as msg_module
from codex_autorunner.integrations.telegram.adapter import (
    TelegramAudio,
    TelegramDocument,
    TelegramForwardOrigin,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.handlers.messages import (
    _CoalescedBuffer,
    _MediaBatchBuffer,
    buffer_coalesced_message,
    buffer_media_batch,
    build_coalesced_message,
    document_is_image,
    handle_media_message,
    handle_message_inner,
    has_batchable_media,
    media_batch_key,
    message_has_media,
    select_file_candidate,
    select_image_candidate,
    select_photo,
    select_voice_candidate,
    should_bypass_topic_queue,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord
from tests.fixtures.telegram_command_helpers import (
    bot_command_entity,
    make_command_spec,
)

# Helper usage: keep message-handler behavior central; shared command helpers are
# only for compact command setup within bypass/dispatch-focused tests.


def _message(**kwargs: object) -> TelegramMessage:
    text = kwargs.pop("text", None)
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text=text,
        date=0,
        is_topic_message=False,
        **kwargs,
    )


class _AsyncNoopLock:
    async def __aenter__(self) -> "_AsyncNoopLock":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = (exc_type, exc, tb)
        return False


def test_build_coalesced_message_replaces_text() -> None:
    message = _message(text="hello", caption="caption")
    buffer = _CoalescedBuffer(message=message, parts=["alpha", "beta"], topic_key="k")
    combined = build_coalesced_message(buffer)
    assert combined.text == "alpha\nbeta"
    assert combined.caption is None
    assert combined.message_id == message.message_id


def test_message_has_media() -> None:
    message = _message()
    assert message_has_media(message) is False
    message = _message(photos=(TelegramPhotoSize("p1", None, 1, 1, 1),))
    assert message_has_media(message) is True


def test_select_photo_prefers_largest_file() -> None:
    photos = [
        TelegramPhotoSize("p1", None, 10, 10, 100),
        TelegramPhotoSize("p2", None, 20, 20, 200),
    ]
    selected = select_photo(photos)
    assert selected is not None
    assert selected.file_id == "p2"


def test_document_is_image_by_mime_type() -> None:
    document = TelegramDocument("d1", None, "file.bin", "image/png", 42)
    assert document_is_image(document) is True


def test_document_is_image_by_suffix() -> None:
    document = TelegramDocument("d1", None, "photo.JPG", None, 42)
    assert document_is_image(document) is True


def test_document_is_not_image() -> None:
    document = TelegramDocument("d1", None, "doc.txt", "text/plain", 42)
    assert document_is_image(document) is False


def test_select_image_candidate_prefers_photo() -> None:
    message = _message(
        photos=(TelegramPhotoSize("p1", None, 10, 10, 100),),
        document=TelegramDocument("d1", None, "photo.png", "image/png", 42),
    )
    candidate = select_image_candidate(message)
    assert candidate is not None
    assert candidate.kind == "photo"
    assert candidate.file_id == "p1"


def test_select_image_candidate_uses_document() -> None:
    message = _message(
        document=TelegramDocument("d1", None, "photo.png", "image/png", 42)
    )
    candidate = select_image_candidate(message)
    assert candidate is not None
    assert candidate.kind == "document"
    assert candidate.file_id == "d1"


def test_select_voice_candidate_prefers_voice() -> None:
    message = _message(voice=TelegramVoice("v1", None, 3, "audio/ogg", 100))
    candidate = select_voice_candidate(message)
    assert candidate is not None
    assert candidate.kind == "voice"
    assert candidate.file_id == "v1"
    assert candidate.mime_type == "audio/ogg"


def test_select_voice_candidate_normalizes_generic_audio_mime_by_suffix() -> None:
    message = _message(
        audio=TelegramAudio(
            "a1",
            None,
            180,
            "clip.ogg",
            "application/octet-stream",
            200,
        )
    )
    candidate = select_voice_candidate(message)
    assert candidate is not None
    assert candidate.kind == "audio"
    assert candidate.mime_type == "audio/ogg"


def test_select_file_candidate_uses_document() -> None:
    message = _message(
        document=TelegramDocument("d1", None, "report.txt", "text/plain", 42)
    )
    candidate = select_file_candidate(message)
    assert candidate is not None
    assert candidate.kind == "file"
    assert candidate.file_id == "d1"


def test_select_file_candidate_ignores_image_document() -> None:
    message = _message(
        document=TelegramDocument("d1", None, "photo.png", "image/png", 42)
    )
    candidate = select_file_candidate(message)
    assert candidate is None


def test_should_bypass_topic_queue_for_interrupt() -> None:
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={},
        _pending_questions={},
    )
    message = _message(text="^C")
    assert should_bypass_topic_queue(handlers, message) is True


def test_should_bypass_topic_queue_for_allow_during_turn() -> None:
    spec = make_command_spec("status", "status", allow_during_turn=True)
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={"status": spec},
        _pending_questions={},
    )
    message = _message(
        text="/status",
        entities=(bot_command_entity("/status"),),
    )
    assert should_bypass_topic_queue(handlers, message) is True


def test_should_not_bypass_topic_queue_without_allow_during_turn() -> None:
    spec = make_command_spec("new", "new", allow_during_turn=False)
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={"new": spec},
        _pending_questions={},
    )
    message = _message(
        text="/new",
        entities=(bot_command_entity("/new"),),
    )
    assert should_bypass_topic_queue(handlers, message) is False


def test_should_bypass_topic_queue_for_custom_answer() -> None:
    pending = types.SimpleNamespace(
        awaiting_custom_input=True,
        chat_id=3,
        thread_id=4,
    )
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={},
        _pending_questions={"req-1": pending},
    )
    message = _message(text="Purple")
    assert should_bypass_topic_queue(handlers, message) is True


def test_should_not_bypass_topic_queue_for_forwarded_command_text() -> None:
    spec = make_command_spec("status", "status", allow_during_turn=True)
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={"status": spec},
        _pending_questions={},
    )
    message = _message(
        text="/status",
        entities=(bot_command_entity("/status"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    assert should_bypass_topic_queue(handlers, message) is False


def test_has_batchable_media_with_photos() -> None:
    message = _message(photos=(TelegramPhotoSize("p1", None, 10, 10, 100),))
    assert has_batchable_media(message) is True


def test_has_batchable_media_with_document_image() -> None:
    message = _message(
        document=TelegramDocument("d1", None, "photo.png", "image/png", 42)
    )
    assert has_batchable_media(message) is True


def test_has_batchable_media_with_document_file() -> None:
    message = _message(
        document=TelegramDocument("d1", None, "report.txt", "text/plain", 42)
    )
    assert has_batchable_media(message) is True


def test_has_batchable_media_without_media() -> None:
    message = _message(text="hello")
    assert has_batchable_media(message) is False


def test_has_batchable_media_with_voice() -> None:
    message = _message(voice=TelegramVoice("v1", None, 3, "audio/ogg", 100))
    assert has_batchable_media(message) is False


def test_audio_message_has_media() -> None:
    message = _message(
        audio=TelegramAudio("a1", None, 180, "song.mp3", "audio/mpeg", 200)
    )
    assert message_has_media(message) is True


@pytest.mark.anyio
async def test_audio_message_bypasses_coalescing_like_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import codex_autorunner.integrations.telegram.handlers.messages as msg_module

    audio_message = _message(
        audio=TelegramAudio("a1", None, 180, "song.mp3", "audio/mpeg", 200)
    )

    handlers = MagicMock()
    handlers._bot_username = "CodexBot"
    handlers._config.media.batch_uploads = True
    handlers._config.media.enabled = True
    handlers._claim_queued_placeholder = MagicMock(return_value=None)
    handlers._delete_message = MagicMock()

    # Mock module-level functions that are awaited
    mock_flush = MagicMock()

    async def async_flush(*args, **kwargs):
        mock_flush(*args, **kwargs)

    monkeypatch.setattr(msg_module, "flush_coalesced_message", async_flush)

    mock_handle_inner = MagicMock()

    async def async_handle_inner(*args, **kwargs):
        mock_handle_inner(*args, **kwargs)

    monkeypatch.setattr(msg_module, "handle_message_inner", async_handle_inner)

    mock_buffer = MagicMock()

    async def async_buffer(*args, **kwargs):
        mock_buffer(*args, **kwargs)

    monkeypatch.setattr(msg_module, "buffer_coalesced_message", async_buffer)

    mock_buffer_media = MagicMock()

    async def async_buffer_media(*args, **kwargs):
        mock_buffer_media(*args, **kwargs)

    monkeypatch.setattr(msg_module, "buffer_media_batch", async_buffer_media)

    await msg_module.handle_message(handlers, audio_message)

    # Audio should bypass, so it flushes and calls inner directly
    mock_flush.assert_called_once_with(handlers, audio_message)
    mock_handle_inner.assert_called_once_with(
        handlers, audio_message, placeholder_id=None
    )
    mock_buffer.assert_not_called()
    mock_buffer_media.assert_not_called()


@pytest.mark.anyio
async def test_forwarded_text_message_bypasses_coalescing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded_message = _message(
        text="/status",
        entities=(bot_command_entity("/status"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )

    timeline: list[tuple[str, object]] = []
    topic_key = "topic-key"
    coalesce_key = f"{topic_key}:user:{forwarded_message.from_user_id}"

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return topic_key

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(batch_uploads=True, enabled=True)
        ),
        _pending_questions={},
        _resolve_topic_key=_resolve_topic_key,
        _coalesce_locks={coalesce_key: _AsyncNoopLock()},
        _coalesced_buffers={
            coalesce_key: _CoalescedBuffer(
                message=_message(text="older"),
                parts=["older"],
                topic_key=topic_key,
            )
        },
        _claim_queued_placeholder=lambda *_args, **_kwargs: None,
        _delete_message=lambda *_args, **_kwargs: None,
    )

    async def async_handle_inner(
        _handlers: object,
        message: TelegramMessage,
        *,
        topic_key: Optional[str] = None,
        placeholder_id: Optional[int] = None,
    ) -> None:
        _ = topic_key
        timeline.append((message.text or "", message.forward_origin))
        assert placeholder_id is None

    monkeypatch.setattr(msg_module, "handle_message_inner", async_handle_inner)

    async def async_buffer(*args, **kwargs):
        raise AssertionError("forwarded text should not be buffered for coalescing")

    monkeypatch.setattr(msg_module, "buffer_coalesced_message", async_buffer)

    await msg_module.handle_message(handlers, forwarded_message)

    assert timeline == [
        ("older", None),
        ("/status", forwarded_message.forward_origin),
    ]


@pytest.mark.anyio
async def test_media_batch_key_with_media_group() -> None:
    async def _resolve_topic_key(chat_id: int, thread_id: int) -> str:
        return f"chat:{chat_id}:thread:{thread_id}"

    handlers = types.SimpleNamespace(_resolve_topic_key=_resolve_topic_key)
    message = _message(
        media_group_id="mg123",
        photos=(TelegramPhotoSize("p1", None, 10, 10, 100),),
    )
    key = await media_batch_key(handlers, message)
    assert key == "chat:3:thread:4:user:5:mg:mg123"


@pytest.mark.anyio
async def test_media_batch_key_without_media_group() -> None:
    async def _resolve_topic_key(chat_id: int, thread_id: int) -> str:
        return f"chat:{chat_id}:thread:{thread_id}"

    handlers = types.SimpleNamespace(_resolve_topic_key=_resolve_topic_key)
    message = _message(
        photos=(TelegramPhotoSize("p1", None, 10, 10, 100),),
    )
    key = await media_batch_key(handlers, message)
    assert key == "chat:3:thread:4:user:5:burst"


@pytest.mark.anyio
async def test_buffer_coalesced_message_does_not_construct_lock_when_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_ctor_calls = 0

    def _counting_lock() -> _AsyncNoopLock:
        nonlocal lock_ctor_calls
        lock_ctor_calls += 1
        return _AsyncNoopLock()

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.messages.asyncio.Lock",
        _counting_lock,
    )

    async def _resolve_topic_key(chat_id: int, thread_id: int) -> str:
        return f"chat:{chat_id}:thread:{thread_id}"

    def _spawn_task(coro: object) -> None:
        coro.close()
        return None

    key = "chat:3:thread:4:user:5"
    handlers = types.SimpleNamespace(
        _resolve_topic_key=_resolve_topic_key,
        _coalesce_locks={key: _AsyncNoopLock()},
        _coalesced_buffers={},
        _touch_cache_timestamp=lambda *_args, **_kwargs: None,
        _spawn_task=_spawn_task,
        _config=types.SimpleNamespace(coalesce_window_seconds=0.5),
    )

    await buffer_coalesced_message(handlers, _message(text="hello"), "hello")
    assert lock_ctor_calls == 0


@pytest.mark.anyio
async def test_flush_coalesced_key_wraps_with_typing_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: list[tuple[object, ...]] = []

    async def _handle_inner(
        _handlers: object,
        message: TelegramMessage,
        *,
        topic_key: Optional[str] = None,
        placeholder_id: Optional[int] = None,
    ) -> None:
        timeline.append(("inner", message.text, topic_key, placeholder_id))

    monkeypatch.setattr(msg_module, "handle_message_inner", _handle_inner)

    async def _begin(chat_id: int, thread_id: Optional[int]) -> None:
        timeline.append(("begin", chat_id, thread_id))

    async def _end(chat_id: int, thread_id: Optional[int]) -> None:
        timeline.append(("end", chat_id, thread_id))

    key = "chat:3:thread:4:user:5"
    handlers = types.SimpleNamespace(
        _coalesce_locks={key: _AsyncNoopLock()},
        _coalesced_buffers={
            key: _CoalescedBuffer(
                message=_message(text="hello"),
                parts=["alpha", "beta"],
                topic_key="topic-key",
                placeholder_id=99,
            )
        },
        _begin_typing_indicator=_begin,
        _end_typing_indicator=_end,
    )

    await msg_module.flush_coalesced_key(handlers, key)

    assert timeline == [
        ("begin", 3, 4),
        ("inner", "alpha\nbeta", "topic-key", 99),
        ("end", 3, 4),
    ]


@pytest.mark.anyio
async def test_buffer_media_batch_does_not_construct_lock_when_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_ctor_calls = 0

    def _counting_lock() -> _AsyncNoopLock:
        nonlocal lock_ctor_calls
        lock_ctor_calls += 1
        return _AsyncNoopLock()

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.messages.asyncio.Lock",
        _counting_lock,
    )

    async def _resolve_topic_key(chat_id: int, thread_id: int) -> str:
        return f"chat:{chat_id}:thread:{thread_id}"

    def _spawn_task(coro: object) -> None:
        coro.close()
        return None

    key = "chat:3:thread:4:user:5:burst"
    handlers = types.SimpleNamespace(
        _resolve_topic_key=_resolve_topic_key,
        _media_batch_locks={key: _AsyncNoopLock()},
        _media_batch_buffers={},
        _touch_cache_timestamp=lambda *_args, **_kwargs: None,
        _spawn_task=_spawn_task,
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(batch_window_seconds=0.5)
        ),
    )

    message = _message(photos=(TelegramPhotoSize("p1", None, 10, 10, 100),))
    await buffer_media_batch(handlers, message)
    assert lock_ctor_calls == 0


@pytest.mark.anyio
async def test_handle_message_preserves_forwarded_media_through_batch_flush() -> None:
    message = _message(
        caption="caption here",
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=12,
        ),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    queued: list[object] = []
    batch_calls: list[dict[str, object]] = []

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _dismiss_review_custom_prompt(*_args, **_kwargs) -> None:
        return

    async def _delete_message(*_args, **_kwargs) -> None:
        return

    async def _handle_media_batch(
        messages: list[TelegramMessage],
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        batch_calls.append(
            {
                "messages": messages,
                "placeholder_id": placeholder_id,
            }
        )

    def _spawn_task(coro: object) -> None:
        coro.close()
        return None

    def _wrap_placeholder_work(
        *,
        chat_id: int,
        placeholder_id: Optional[int],
        work: object,
    ) -> object:
        _ = (chat_id, placeholder_id)
        return work()

    def _enqueue_topic_work(_key: str, wrapped: object) -> None:
        queued.append(wrapped)

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(
                enabled=True,
                batch_uploads=True,
                batch_window_seconds=0.5,
            )
        ),
        _pending_questions={},
        _resume_options={},
        _bind_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _dismiss_review_custom_prompt=_dismiss_review_custom_prompt,
        _claim_queued_placeholder=lambda *_args, **_kwargs: None,
        _delete_message=_delete_message,
        _resolve_topic_key=_resolve_topic_key,
        _coalesce_locks={},
        _coalesced_buffers={},
        _media_batch_locks={},
        _media_batch_buffers={},
        _touch_cache_timestamp=lambda *_args, **_kwargs: None,
        _spawn_task=_spawn_task,
        _wrap_placeholder_work=_wrap_placeholder_work,
        _enqueue_topic_work=_enqueue_topic_work,
        _handle_media_batch=_handle_media_batch,
    )

    await msg_module.handle_message(handlers, message)

    key = await media_batch_key(handlers, message)
    assert key in handlers._media_batch_buffers
    await msg_module.flush_media_batch_key(handlers, key)
    assert len(queued) == 1
    await queued[0]

    assert len(batch_calls) == 1
    forwarded = batch_calls[0]["messages"][0].forward_origin
    assert forwarded is not None
    assert forwarded.source_label == "Ops"
    assert forwarded.message_id == 9


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("locks_attr", "guard_attr"),
    [
        ("_coalesce_locks", "_coalesce_locks_guard"),
        ("_media_batch_locks", "_media_batch_locks_guard"),
    ],
)
async def test_ensure_key_lock_returns_same_instance_for_concurrent_callers(
    locks_attr: str, guard_attr: str
) -> None:
    handlers = types.SimpleNamespace(**{locks_attr: {}})
    key = "chat:3:thread:4:user:5"

    locks = await asyncio.gather(
        *[
            msg_module._ensure_key_lock(
                handlers,
                locks_attr=locks_attr,
                guard_attr=guard_attr,
                key=key,
            )
            for _ in range(10)
        ]
    )
    first = locks[0]
    assert all(lock is first for lock in locks)
    assert getattr(handlers, locks_attr)[key] is first


@pytest.mark.anyio
async def test_handle_message_allows_forwarded_custom_question_command_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message(
        text="/workspace/repo",
        entities=(bot_command_entity("/workspace/repo"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    policy_flags: list[bool] = []
    custom_calls: list[int] = []

    async def _handle_custom_text_input(
        _handlers: object, custom_message: TelegramMessage
    ) -> bool:
        custom_calls.append(custom_message.message_id)
        return True

    monkeypatch.setattr(
        msg_module, "handle_custom_text_input", _handle_custom_text_input
    )

    async def _delete_message(*_args, **_kwargs) -> None:
        raise AssertionError("forwarded custom input should not be rejected")

    pending = types.SimpleNamespace(
        awaiting_custom_input=True,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
    )
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _pending_questions={"req-1": pending},
        _claim_queued_placeholder=lambda *_args, **_kwargs: None,
        _delete_message=_delete_message,
        _evaluate_collaboration_message_policy=lambda _message, *, text, is_explicit_command: (
            policy_flags.append(is_explicit_command)
            or types.SimpleNamespace(
                command_allowed=not is_explicit_command,
                should_start_turn=True,
            )
        ),
    )

    await msg_module.handle_message(handlers, message)

    assert policy_flags == [False]
    assert custom_calls == [message.message_id]


@pytest.mark.anyio
async def test_handle_message_inner_allows_forwarded_pending_review_custom_text() -> (
    None
):
    message = _message(
        text="/workspace/repo",
        entities=(bot_command_entity("/workspace/repo"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    policy_flags: list[bool] = []
    review_custom_calls: list[dict[str, object]] = []
    deleted: list[tuple[object, ...]] = []
    runtime = object()

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _handle_pending_review_commit(*_args, **_kwargs) -> bool:
        return False

    async def _handle_pending_review_custom(
        key: str,
        _message: TelegramMessage,
        runtime_arg: object,
        command: object,
        raw_text: str,
        raw_caption: str,
    ) -> bool:
        review_custom_calls.append(
            {
                "key": key,
                "runtime_matches": runtime_arg is runtime,
                "command": command,
                "raw_text": raw_text,
                "raw_caption": raw_caption,
            }
        )
        return True

    async def _delete_message(*args, **_kwargs) -> None:
        deleted.append(args)

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="all"),
        _resume_options={},
        _bind_options={},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={"topic-key": object()},
        _resolve_topic_key=_resolve_topic_key,
        _handle_pending_resume=lambda *_args, **_kwargs: False,
        _handle_pending_bind=lambda *_args, **_kwargs: False,
        _handle_pending_review_commit=_handle_pending_review_commit,
        _handle_pending_review_custom=_handle_pending_review_custom,
        _dismiss_review_custom_prompt=lambda *_args, **_kwargs: None,
        _delete_message=_delete_message,
        _command_specs={},
        _evaluate_collaboration_message_policy=lambda _message, *, text, is_explicit_command: (
            policy_flags.append(is_explicit_command)
            or types.SimpleNamespace(
                command_allowed=not is_explicit_command,
                should_start_turn=True,
            )
        ),
    )

    await handle_message_inner(handlers, message, placeholder_id=99)

    assert policy_flags == [False]
    assert len(review_custom_calls) == 1
    assert review_custom_calls[0]["key"] == "topic-key"
    assert review_custom_calls[0]["runtime_matches"] is True
    assert review_custom_calls[0]["command"] is None
    assert review_custom_calls[0]["raw_text"] == "/workspace/repo"
    assert deleted == [(message.chat_id, 99)]


@pytest.mark.anyio
async def test_handle_message_inner_allows_forwarded_pending_bind_text() -> None:
    message = _message(
        text="/workspace/repo",
        entities=(bot_command_entity("/workspace/repo"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    policy_flags: list[bool] = []
    bind_calls: list[tuple[str, str, Optional[int]]] = []
    deleted: list[tuple[object, ...]] = []
    runtime = object()

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _delete_message(*args, **_kwargs) -> None:
        deleted.append(args)

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="all"),
        _resume_options={},
        _bind_options={"topic-key": object()},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _resolve_topic_key=_resolve_topic_key,
        _handle_pending_resume=lambda *_args, **_kwargs: False,
        _handle_pending_bind=lambda key, text, *, user_id=None: (
            bind_calls.append((key, text, user_id)) or True
        ),
        _handle_pending_review_commit=lambda *_args, **_kwargs: False,
        _handle_pending_review_custom=lambda *_args, **_kwargs: False,
        _dismiss_review_custom_prompt=lambda *_args, **_kwargs: None,
        _delete_message=_delete_message,
        _command_specs={},
        _evaluate_collaboration_message_policy=lambda _message, *, text, is_explicit_command: (
            policy_flags.append(is_explicit_command)
            or types.SimpleNamespace(
                command_allowed=not is_explicit_command,
                should_start_turn=True,
            )
        ),
    )

    await handle_message_inner(handlers, message, placeholder_id=99)

    assert policy_flags == [False]
    assert bind_calls == [("topic-key", "/workspace/repo", message.from_user_id)]
    assert deleted == [(message.chat_id, 99)]


@pytest.mark.anyio
async def test_handle_message_inner_allows_forwarded_pending_resume_text() -> None:
    message = _message(
        text="/workspace/repo",
        entities=(bot_command_entity("/workspace/repo"),),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    policy_flags: list[bool] = []
    resume_calls: list[tuple[str, str, Optional[int]]] = []
    deleted: list[tuple[object, ...]] = []
    runtime = object()

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _delete_message(*args, **_kwargs) -> None:
        deleted.append(args)

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="all"),
        _resume_options={"topic-key": object()},
        _bind_options={},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _resolve_topic_key=_resolve_topic_key,
        _handle_pending_resume=lambda key, text, *, user_id=None: (
            resume_calls.append((key, text, user_id)) or True
        ),
        _handle_pending_bind=lambda *_args, **_kwargs: False,
        _handle_pending_review_commit=lambda *_args, **_kwargs: False,
        _handle_pending_review_custom=lambda *_args, **_kwargs: False,
        _dismiss_review_custom_prompt=lambda *_args, **_kwargs: None,
        _delete_message=_delete_message,
        _command_specs={},
        _evaluate_collaboration_message_policy=lambda _message, *, text, is_explicit_command: (
            policy_flags.append(is_explicit_command)
            or types.SimpleNamespace(
                command_allowed=not is_explicit_command,
                should_start_turn=True,
            )
        ),
    )

    await handle_message_inner(handlers, message, placeholder_id=99)

    assert policy_flags == [False]
    assert resume_calls == [("topic-key", "/workspace/repo", message.from_user_id)]
    assert deleted == [(message.chat_id, 99)]


@pytest.mark.anyio
async def test_handle_message_inner_uses_formatted_forwarded_prompt_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message(
        text="deploy this",
        forward_origin=TelegramForwardOrigin(
            source_label="Ops", message_id=9, is_automatic=True
        ),
    )
    captured: dict[str, object] = {}
    runtime = object()

    class _IngressStub:
        async def submit_message(
            self,
            request,
            *,
            resolve_paused_flow_target,
            submit_flow_reply,
            submit_thread_message,
        ) -> None:
            _ = (resolve_paused_flow_target, submit_flow_reply)
            captured["prompt_text"] = request.prompt_text
            await submit_thread_message(request)

    monkeypatch.setattr(
        msg_module,
        "build_surface_orchestration_ingress",
        lambda event_sink: _IngressStub(),
    )

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

        async def get_topic(self, _key: str) -> object:
            return types.SimpleNamespace(pma_enabled=True, workspace_path=".")

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _handle_pending_review_commit(*_args, **_kwargs) -> bool:
        return False

    async def _handle_pending_review_custom(*_args, **_kwargs) -> bool:
        return False

    async def _dismiss_review_custom_prompt(*_args, **_kwargs) -> None:
        return

    async def _delete_message(*_args, **_kwargs) -> None:
        return

    async def _handle_normal_message(
        _message: TelegramMessage,
        runtime_arg: object,
        *,
        text_override: str,
        placeholder_id: Optional[int] = None,
    ) -> None:
        captured["runtime_matches"] = runtime_arg is runtime
        captured["text_override"] = text_override
        captured["placeholder_id"] = placeholder_id

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="all"),
        _resume_options={},
        _bind_options={},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _resolve_topic_key=_resolve_topic_key,
        _handle_pending_resume=lambda *_args, **_kwargs: False,
        _handle_pending_bind=lambda *_args, **_kwargs: False,
        _handle_pending_review_commit=_handle_pending_review_commit,
        _handle_pending_review_custom=_handle_pending_review_custom,
        _dismiss_review_custom_prompt=_dismiss_review_custom_prompt,
        _delete_message=_delete_message,
        _command_specs={},
        _handle_normal_message=_handle_normal_message,
    )

    await handle_message_inner(handlers, message, placeholder_id=99)

    expected = "Forwarded message (automatic) from Ops [message 9]:\n" "deploy this"
    assert captured["prompt_text"] == expected
    assert captured["text_override"] == expected
    assert captured["runtime_matches"] is True
    assert captured["placeholder_id"] == 99


@pytest.mark.anyio
async def test_handle_media_message_uses_formatted_forwarded_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message(
        text="caption here",
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=12,
        ),
        forward_origin=TelegramForwardOrigin(source_label="Ops", message_id=9),
    )
    captured: dict[str, object] = {}
    runtime = object()

    class _IngressStub:
        async def submit_message(
            self,
            request,
            *,
            resolve_paused_flow_target,
            submit_flow_reply,
            submit_thread_message,
        ) -> None:
            _ = (resolve_paused_flow_target, submit_flow_reply)
            captured["prompt_text"] = request.prompt_text
            await submit_thread_message(request)

    monkeypatch.setattr(
        msg_module,
        "build_surface_orchestration_ingress",
        lambda event_sink: _IngressStub(),
    )

    async def _handle_file_message(
        _message: TelegramMessage,
        runtime_arg: object,
        record: object,
        file_candidate: object,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        _ = (record, file_candidate)
        captured["runtime_matches"] = runtime_arg is runtime
        captured["caption_text"] = caption_text
        captured["placeholder_id"] = placeholder_id

    class _RouterStub:
        async def get_topic(self, _key: str) -> object:
            return TelegramTopicRecord(workspace_path=".", pma_enabled=True)

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    handlers = types.SimpleNamespace(
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(
                enabled=True,
                max_image_bytes=1024,
                max_file_bytes=1024,
                max_voice_bytes=1024,
                images=True,
                voice=True,
                files=True,
            )
        ),
        _router=_RouterStub(),
        _hub_root=".",
        _resolve_topic_key=_resolve_topic_key,
        _ticket_flow_pause_targets={},
        _logger=types.SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        _with_conversation_id=lambda text, **_kwargs: text,
        _handle_file_message=_handle_file_message,
    )

    await handle_media_message(
        handlers,
        message,
        runtime=runtime,
        caption_text="caption here",
        placeholder_id=99,
    )

    expected = "Forwarded message from Ops [message 9]:\ncaption here"
    assert captured["prompt_text"] == expected
    assert captured["caption_text"] == expected
    assert captured["runtime_matches"] is True
    assert captured["placeholder_id"] == 99


@pytest.mark.anyio
async def test_handle_message_inner_paused_flow_accepts_free_text_without_reply_to() -> (
    None
):
    message = _message(text="yes, continue")
    sent: list[str] = []
    reply_calls: list[dict[str, object]] = []
    resume_calls: list[tuple[str, str]] = []
    run_id = "00000000-0000-0000-0000-000000000123"

    runtime = object()

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

        async def get_topic(self, _key: str) -> object:
            return types.SimpleNamespace(pma_enabled=False, workspace_path=".")

    async def _write_user_reply(
        _workspace_root,
        _run_id: str,
        _run_record,
        _message: TelegramMessage,
        text: str,
        _files=None,
    ) -> tuple[bool, str]:
        reply_calls.append({"run_id": _run_id, "text": text})
        return True, "Reply archived (seq 0001)."

    class _TicketFlowBridgeStub:
        async def auto_resume_run(self, workspace_root, paused_run_id: str) -> None:
            resume_calls.append((str(workspace_root), paused_run_id))

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _handle_interrupt(*_args, **_kwargs) -> None:
        return

    async def _handle_pending_review_commit(*_args, **_kwargs) -> bool:
        return False

    async def _handle_pending_review_custom(*_args, **_kwargs) -> bool:
        return False

    async def _dismiss_review_custom_prompt(*_args, **_kwargs) -> None:
        return

    async def _delete_message(*_args, **_kwargs) -> None:
        return

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="mentions"),
        _resume_options={},
        _bind_options={},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _ticket_flow_pause_targets={},
        _ticket_flow_bridge=_TicketFlowBridgeStub(),
        _handle_pending_resume=lambda *_args, **_kwargs: False,
        _handle_pending_bind=lambda *_args, **_kwargs: False,
        _resolve_topic_key=_resolve_topic_key,
        _handle_interrupt=_handle_interrupt,
        _handle_pending_review_commit=_handle_pending_review_commit,
        _handle_pending_review_custom=_handle_pending_review_custom,
        _dismiss_review_custom_prompt=_dismiss_review_custom_prompt,
        _command_specs={},
        _get_paused_ticket_flow=lambda *_args, **_kwargs: (
            run_id,
            types.SimpleNamespace(id=run_id, input_data={}),
        ),
        _write_user_reply_from_telegram=_write_user_reply,
        _delete_message=_delete_message,
    )

    async def _send_message(
        _chat_id: int,
        text: str,
        *,
        thread_id=None,
        reply_to=None,
    ) -> None:
        _ = (thread_id, reply_to)
        sent.append(text)

    handlers._send_message = _send_message

    await handle_message_inner(handlers, message)

    assert reply_calls == [{"run_id": run_id, "text": "yes, continue"}]
    assert len(resume_calls) == 1
    assert resume_calls[0][1] == run_id
    assert sent == ["Reply archived (seq 0001)."]


@pytest.mark.anyio
async def test_handle_message_inner_paused_flow_with_media_uses_media_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message(
        text="caption here",
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=12,
        ),
    )
    sent: list[str] = []
    queued: list[object] = []
    run_id = "00000000-0000-0000-0000-000000000456"
    runtime = object()
    media_calls: list[dict[str, object]] = []

    async def _fake_handle_media_message(
        _handlers: object,
        msg: TelegramMessage,
        runtime_arg: object,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        media_calls.append(
            {
                "message_id": msg.message_id,
                "caption_text": caption_text,
                "placeholder_id": placeholder_id,
                "runtime_is_same": runtime_arg is runtime,
            }
        )

    monkeypatch.setattr(
        "codex_autorunner.integrations.telegram.handlers.messages.handle_media_message",
        _fake_handle_media_message,
    )

    class _RouterStub:
        def runtime_for(self, _key: str) -> object:
            return runtime

        async def get_topic(self, _key: str) -> object:
            return types.SimpleNamespace(pma_enabled=False, workspace_path=".")

    async def _write_user_reply(*_args, **_kwargs) -> tuple[bool, str]:
        raise AssertionError("paused text branch should not run for media messages")

    class _TicketFlowBridgeStub:
        async def auto_resume_run(self, workspace_root, paused_run_id: str) -> None:
            _ = (workspace_root, paused_run_id)

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _handle_interrupt(*_args, **_kwargs) -> None:
        return

    async def _handle_pending_review_commit(*_args, **_kwargs) -> bool:
        return False

    async def _handle_pending_review_custom(*_args, **_kwargs) -> bool:
        return False

    async def _dismiss_review_custom_prompt(*_args, **_kwargs) -> None:
        return

    async def _delete_message(*_args, **_kwargs) -> None:
        return

    def _enqueue_topic_work(_key: str, wrapped: object) -> None:
        queued.append(wrapped)

    def _wrap_placeholder_work(
        *,
        chat_id: int,
        placeholder_id: Optional[int],
        work: object,
    ) -> object:
        _ = (chat_id, placeholder_id)
        return work()

    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _router=_RouterStub(),
        _config=types.SimpleNamespace(trigger_mode="all"),
        _resume_options={},
        _bind_options={},
        _flow_run_options={},
        _agent_options={},
        _model_options={},
        _model_pending={},
        _review_commit_options={},
        _review_commit_subjects={},
        _pending_review_custom={},
        _ticket_flow_pause_targets={},
        _ticket_flow_bridge=_TicketFlowBridgeStub(),
        _handle_pending_resume=lambda *_args, **_kwargs: False,
        _handle_pending_bind=lambda *_args, **_kwargs: False,
        _resolve_topic_key=_resolve_topic_key,
        _handle_interrupt=_handle_interrupt,
        _handle_pending_review_commit=_handle_pending_review_commit,
        _handle_pending_review_custom=_handle_pending_review_custom,
        _dismiss_review_custom_prompt=_dismiss_review_custom_prompt,
        _command_specs={},
        _get_paused_ticket_flow=lambda *_args, **_kwargs: (
            run_id,
            types.SimpleNamespace(id=run_id, input_data={}),
        ),
        _write_user_reply_from_telegram=_write_user_reply,
        _delete_message=_delete_message,
        _enqueue_topic_work=_enqueue_topic_work,
        _wrap_placeholder_work=_wrap_placeholder_work,
    )

    async def _send_message(
        _chat_id: int,
        text: str,
        *,
        thread_id=None,
        reply_to=None,
    ) -> None:
        _ = (thread_id, reply_to)
        sent.append(text)

    handlers._send_message = _send_message

    await handle_message_inner(handlers, message)
    assert len(queued) == 1
    await queued[0]

    assert media_calls
    assert media_calls[0]["caption_text"] == "caption here"
    assert media_calls[0]["runtime_is_same"] is True
    assert sent == []


@pytest.mark.anyio
async def test_handle_media_message_paused_flow_archives_non_reply_media_and_resumes() -> (
    None
):
    message = _message(
        text="caption here",
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=12,
        ),
    )
    sent: list[str] = []
    archived: list[dict[str, object]] = []
    resumed: list[tuple[str, str]] = []
    run_id = "00000000-0000-0000-0000-000000000789"

    class _RouterStub:
        async def get_topic(self, _key: str) -> object:
            return types.SimpleNamespace(pma_enabled=False, workspace_path=".")

    class _TicketFlowBridgeStub:
        async def auto_resume_run(self, workspace_root, paused_run_id: str) -> None:
            resumed.append((str(workspace_root), paused_run_id))

    class _BotStub:
        async def get_file(self, _file_id: str) -> object:
            return types.SimpleNamespace(file_path="files/doc-1")

        async def download_file(
            self, _file_path: str, *, max_size_bytes: Optional[int] = None
        ) -> bytes:
            _ = max_size_bytes
            return b"doc-bytes"

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    async def _write_user_reply(
        _workspace_root,
        _run_id: str,
        _run_record,
        _message: TelegramMessage,
        text: str,
        files=None,
    ) -> tuple[bool, str]:
        archived.append({"run_id": _run_id, "text": text, "files": files or []})
        return True, "Reply archived (seq 0002)."

    async def _send_message(
        _chat_id: int,
        text: str,
        *,
        thread_id=None,
        reply_to=None,
    ) -> None:
        _ = (thread_id, reply_to)
        sent.append(text)

    handlers = types.SimpleNamespace(
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(
                enabled=True,
                max_image_bytes=1024,
                max_file_bytes=1024,
                images=True,
                voice=True,
                files=True,
            )
        ),
        _router=_RouterStub(),
        _ticket_flow_pause_targets={},
        _get_paused_ticket_flow=lambda *_args, **_kwargs: (
            run_id,
            types.SimpleNamespace(id=run_id, input_data={}),
        ),
        _bot=_BotStub(),
        _logger=types.SimpleNamespace(debug=lambda *_args, **_kwargs: None),
        _write_user_reply_from_telegram=_write_user_reply,
        _ticket_flow_bridge=_TicketFlowBridgeStub(),
        _resolve_topic_key=_resolve_topic_key,
        _send_message=_send_message,
        _with_conversation_id=lambda text, **_kwargs: text,
    )

    await handle_media_message(
        handlers,
        message,
        runtime=object(),
        caption_text="caption here",
    )

    assert archived and archived[0]["run_id"] == run_id
    assert archived[0]["text"] == "caption here"
    assert archived[0]["files"] == [("notes.txt", b"doc-bytes")]
    assert resumed
    assert resumed[0][1] == run_id
    assert sent == ["Reply archived (seq 0002)."]


@pytest.mark.anyio
async def test_handle_media_message_ignores_paused_flow_for_pma_topics() -> None:
    message = _message(
        text="caption here",
        document=TelegramDocument(
            file_id="doc-1",
            file_unique_id=None,
            file_name="notes.txt",
            mime_type="text/plain",
            file_size=12,
        ),
    )
    sent: list[str] = []
    file_calls: list[dict[str, object]] = []

    class _RouterStub:
        async def get_topic(self, _key: str) -> object:
            return TelegramTopicRecord(pma_enabled=True)

    async def _resolve_topic_key(*_args, **_kwargs) -> str:
        return "topic-key"

    def _get_paused_ticket_flow(*_args, **_kwargs) -> object:
        raise AssertionError("paused flow lookup should not run for PMA topics")

    async def _write_user_reply(*_args, **_kwargs) -> tuple[bool, str]:
        raise AssertionError("paused flow archival should not run for PMA topics")

    async def _handle_file_message(
        _message: TelegramMessage,
        _runtime,
        record,
        file_candidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        _ = placeholder_id
        file_calls.append(
            {
                "record": record,
                "candidate_file_id": file_candidate.file_id,
                "caption_text": caption_text,
            }
        )

    async def _send_message(
        _chat_id: int,
        text: str,
        *,
        thread_id=None,
        reply_to=None,
    ) -> None:
        _ = (thread_id, reply_to)
        sent.append(text)

    handlers = types.SimpleNamespace(
        _config=types.SimpleNamespace(
            media=types.SimpleNamespace(
                enabled=True,
                max_image_bytes=1024,
                max_file_bytes=1024,
                images=True,
                voice=True,
                files=True,
            )
        ),
        _router=_RouterStub(),
        _hub_root=".",
        _ticket_flow_pause_targets={},
        _get_paused_ticket_flow=_get_paused_ticket_flow,
        _write_user_reply_from_telegram=_write_user_reply,
        _resolve_topic_key=_resolve_topic_key,
        _send_message=_send_message,
        _with_conversation_id=lambda text, **_kwargs: text,
        _handle_file_message=_handle_file_message,
    )

    await handle_media_message(
        handlers,
        message,
        runtime=object(),
        caption_text="caption here",
    )

    assert sent == []
    assert len(file_calls) == 1
    assert file_calls[0]["candidate_file_id"] == "doc-1"
    assert file_calls[0]["caption_text"] == "caption here"
    record = file_calls[0]["record"]
    assert bool(getattr(record, "pma_enabled", False)) is True
    assert getattr(record, "workspace_path", None) == "."


def test_media_batch_buffer_defaults() -> None:
    buffer = _MediaBatchBuffer(topic_key="k", messages=[])
    assert buffer.task is None
    assert buffer.media_group_id is None
    assert buffer.created_at == 0.0
