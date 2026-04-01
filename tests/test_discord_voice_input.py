from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.voice.service import VoicePermanentError, VoiceTransientError


def _load_discord_service_module():
    root = Path(__file__).resolve().parents[1] / "src" / "codex_autorunner"

    for name, path in (
        ("codex_autorunner.integrations.chat", root / "integrations" / "chat"),
        ("codex_autorunner.integrations.discord", root / "integrations" / "discord"),
    ):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package

    return importlib.import_module("codex_autorunner.integrations.discord.service")


def _make_service(*, voice_service):
    discord_service = _load_discord_service_module()
    service = object.__new__(discord_service.DiscordBotService)
    service._logger = logging.getLogger("test.discord.voice")
    service._config = SimpleNamespace(
        media=SimpleNamespace(enabled=True, voice=True, max_voice_bytes=10_000),
        max_message_length=2_000,
    )
    service._voice_service_for_workspace = lambda workspace_root: (
        voice_service,
        SimpleNamespace(provider="openai_whisper"),
    )
    return service


def test_build_attachment_filename_normalizes_generic_audio_suffix():
    service = _make_service(voice_service=None)
    attachment = SimpleNamespace(
        kind="audio",
        file_name="voice-message.bin",
        mime_type="application/octet-stream",
        source_url="https://cdn.discordapp.com/attachments/1/2",
    )

    file_name = service._build_attachment_filename(attachment, index=1)

    assert file_name.endswith(".ogg")


@pytest.mark.asyncio
async def test_with_attachment_context_normalizes_discord_voice_transcription_metadata(
    tmp_path: Path,
):
    class _FakeRest:
        async def download_attachment(
            self,
            *,
            url: str,
            max_size_bytes: int | None = None,
        ):
            return b"voice-bytes"

    class _FakeVoiceService:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        async def transcribe_async(self, data: bytes, **kwargs):
            self.calls.append({"data": data, **kwargs})
            return {"text": "hello from discord"}

    voice_service = _FakeVoiceService()
    service = _make_service(voice_service=voice_service)
    service._rest = _FakeRest()

    attachment = SimpleNamespace(
        kind="audio",
        file_id="voice-1",
        file_name="voice-message.bin",
        mime_type="application/octet-stream",
        size_bytes=11,
        source_url="https://cdn.discordapp.com/attachments/1/2",
    )

    prompt_text, saved_count, failed_count, transcript_message, native_input_items = (
        await service._with_attachment_context(
            prompt_text="",
            workspace_root=tmp_path,
            attachments=(attachment,),
            channel_id="123",
        )
    )

    assert saved_count == 1
    assert failed_count == 0
    assert native_input_items is None
    assert "hello from discord" in (transcript_message or "")
    assert "Transcript: hello from discord" in prompt_text

    assert len(voice_service.calls) == 1
    call = voice_service.calls[0]
    assert call["data"] == b"voice-bytes"
    assert str(call["filename"]).endswith(".ogg")
    assert call["content_type"] == "audio/ogg"


@pytest.mark.asyncio
async def test_transcribe_voice_attachment_returns_voice_user_message(tmp_path: Path):
    class _FailingVoiceService:
        async def transcribe_async(self, data: bytes, **kwargs):
            raise VoicePermanentError(
                "missing_api_key",
                "OpenAI Whisper provider requires API key env 'OPENAI_API_KEY' to be set.",
                user_message=(
                    "Voice transcription failed: missing API key. "
                    "Set OPENAI_API_KEY or install a local whisper provider."
                ),
            )

    service = _make_service(voice_service=_FailingVoiceService())
    transcript, warning = await service._transcribe_voice_attachment(
        workspace_root=tmp_path,
        channel_id="123",
        attachment=SimpleNamespace(
            kind="audio",
            file_id="voice-2",
            file_name="voice-message.ogg",
            mime_type="audio/ogg",
            source_url="https://cdn.discordapp.com/attachments/1/2",
        ),
        data=b"voice-bytes",
        file_name="voice-message.ogg",
        mime_type="audio/ogg",
    )

    assert transcript is None
    assert (
        warning
        == "Voice transcription failed: missing API key. Set OPENAI_API_KEY or install a local whisper provider."
    )


@pytest.mark.asyncio
async def test_transcribe_voice_attachment_uses_detail_for_transient_errors(
    tmp_path: Path,
):
    class _FailingVoiceService:
        async def transcribe_async(self, data: bytes, **kwargs):
            raise VoiceTransientError(
                "rate_limited",
                "OpenAI rate limited the request; wait a moment and try again.",
                user_message="Voice transcription rate limited. Retrying...",
            )

    service = _make_service(voice_service=_FailingVoiceService())
    transcript, warning = await service._transcribe_voice_attachment(
        workspace_root=tmp_path,
        channel_id="123",
        attachment=SimpleNamespace(
            kind="audio",
            file_id="voice-3",
            file_name="voice-message.ogg",
            mime_type="audio/ogg",
            source_url="https://cdn.discordapp.com/attachments/1/2",
        ),
        data=b"voice-bytes",
        file_name="voice-message.ogg",
        mime_type="audio/ogg",
    )

    assert transcript is None
    assert warning == "OpenAI rate limited the request; wait a moment and try again."


@pytest.mark.asyncio
async def test_with_attachment_context_uses_effective_provider_for_disclaimer(
    tmp_path: Path,
):
    discord_service = _load_discord_service_module()

    class _FakeRest:
        async def download_attachment(
            self,
            *,
            url: str,
            max_size_bytes: int | None = None,
        ):
            return b"voice-bytes"

    class _FakeVoiceService:
        async def transcribe_async(self, data: bytes, **kwargs):
            return {"text": "hello from local whisper"}

        def effective_provider_name(self) -> str:
            return "local_whisper"

    service = _make_service(voice_service=_FakeVoiceService())
    service._rest = _FakeRest()

    attachment = SimpleNamespace(
        kind="audio",
        file_id="voice-4",
        file_name="voice-message.bin",
        mime_type="application/octet-stream",
        size_bytes=11,
        source_url="https://cdn.discordapp.com/attachments/1/2",
    )

    prompt_text, saved_count, failed_count, transcript_message, native_input_items = (
        await service._with_attachment_context(
            prompt_text="",
            workspace_root=tmp_path,
            attachments=(attachment,),
            channel_id="123",
        )
    )

    assert saved_count == 1
    assert failed_count == 0
    assert native_input_items is None
    assert transcript_message == "User:\nhello from local whisper"
    assert discord_service.DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER not in prompt_text
