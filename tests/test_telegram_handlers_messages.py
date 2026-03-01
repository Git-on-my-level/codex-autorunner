import types

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramAudio,
    TelegramDocument,
    TelegramMessage,
    TelegramPhotoSize,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.handlers.messages import (
    _CoalescedBuffer,
    _MediaBatchBuffer,
    build_coalesced_message,
    document_is_image,
    has_batchable_media,
    media_batch_key,
    message_has_media,
    select_file_candidate,
    select_image_candidate,
    select_photo,
    select_voice_candidate,
    should_bypass_topic_queue,
)
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


def test_media_batch_buffer_defaults() -> None:
    buffer = _MediaBatchBuffer(topic_key="k", messages=[])
    assert buffer.task is None
    assert buffer.media_group_id is None
    assert buffer.created_at == 0.0
