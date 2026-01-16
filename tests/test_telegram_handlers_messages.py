import types

from codex_autorunner.integrations.telegram.adapter import (
    TelegramDocument,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramPhotoSize,
    TelegramVoice,
)
from codex_autorunner.integrations.telegram.handlers.commands import CommandSpec
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
    )
    message = _message(text="^C")
    assert should_bypass_topic_queue(handlers, message) is True


def test_should_bypass_topic_queue_for_allow_during_turn() -> None:
    spec = CommandSpec(
        name="status",
        description="status",
        handler=lambda _message, _args, _runtime: None,
        allow_during_turn=True,
    )
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={"status": spec},
    )
    message = _message(
        text="/status",
        entities=(TelegramMessageEntity("bot_command", 0, len("/status")),),
    )
    assert should_bypass_topic_queue(handlers, message) is True


def test_should_not_bypass_topic_queue_without_allow_during_turn() -> None:
    spec = CommandSpec(
        name="new",
        description="new",
        handler=lambda _message, _args, _runtime: None,
        allow_during_turn=False,
    )
    handlers = types.SimpleNamespace(
        _bot_username="CodexBot",
        _command_specs={"new": spec},
    )
    message = _message(
        text="/new",
        entities=(TelegramMessageEntity("bot_command", 0, len("/new")),),
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


def test_media_batch_key_with_media_group() -> None:
    handlers = types.SimpleNamespace(
        _resolve_topic_key=lambda chat_id, thread_id: f"chat:{chat_id}:thread:{thread_id}",
    )
    message = _message(
        media_group_id="mg123",
        photos=(TelegramPhotoSize("p1", None, 10, 10, 100),),
    )
    key = media_batch_key(handlers, message)
    assert key == "chat:3:thread:4:user:5:mg:mg123"


def test_media_batch_key_without_media_group() -> None:
    handlers = types.SimpleNamespace(
        _resolve_topic_key=lambda chat_id, thread_id: f"chat:{chat_id}:thread:{thread_id}",
    )
    message = _message(
        photos=(TelegramPhotoSize("p1", None, 10, 10, 100),),
    )
    key = media_batch_key(handlers, message)
    assert key == "chat:3:thread:4:user:5:burst"


def test_media_batch_buffer_defaults() -> None:
    buffer = _MediaBatchBuffer(topic_key="k", messages=[])
    assert buffer.task is None
    assert buffer.media_group_id is None
    assert buffer.created_at == 0.0
