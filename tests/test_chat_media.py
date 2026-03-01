from codex_autorunner.integrations.chat.media import (
    audio_content_type_for_input,
    audio_extension_for_input,
    is_audio_mime_or_path,
)


def test_is_audio_mime_or_path_with_duration_and_generic_mime() -> None:
    assert (
        is_audio_mime_or_path(
            mime_type="application/octet-stream",
            file_name="voice-message",
            source_url="https://cdn.discordapp.com/attachments/12345",
            duration_seconds=3,
        )
        is True
    )


def test_is_audio_mime_or_path_respects_explicit_non_audio_mime() -> None:
    assert (
        is_audio_mime_or_path(
            mime_type="image/jpeg",
            file_name="voice-message.ogg",
            source_url=None,
            duration_seconds=4,
        )
        is False
    )


def test_audio_content_type_for_input_prefers_extension_for_generic_mime() -> None:
    assert (
        audio_content_type_for_input(
            mime_type="application/octet-stream",
            file_name="voice-note.ogg",
            source_url=None,
        )
        == "audio/ogg"
    )


def test_audio_content_type_for_input_returns_none_when_unknown() -> None:
    assert (
        audio_content_type_for_input(
            mime_type="application/octet-stream",
            file_name="voice-note",
            source_url="https://cdn.discordapp.com/attachments/no-ext",
        )
        is None
    )


def test_audio_extension_for_input_uses_url_suffix() -> None:
    assert (
        audio_extension_for_input(
            mime_type=None,
            file_name=None,
            source_url="https://cdn.discordapp.com/attachments/voice.opus?download=1",
        )
        == ".opus"
    )
