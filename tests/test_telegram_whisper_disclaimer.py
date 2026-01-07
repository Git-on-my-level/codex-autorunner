import asyncio
from pathlib import Path

from codex_autorunner.telegram_bot import (
    TelegramBotConfig,
    TelegramBotService,
    WHISPER_TRANSCRIPT_DISCLAIMER,
)
from codex_autorunner.voice import VoiceConfig


def _make_config(root: Path) -> TelegramBotConfig:
    raw = {
        "enabled": True,
        "mode": "polling",
        "allowed_chat_ids": [123],
        "allowed_user_ids": [456],
        "require_topics": False,
        "app_server_command": ["echo", "ok"],
    }
    env = {
        "CAR_TELEGRAM_BOT_TOKEN": "test-token",
        "CAR_TELEGRAM_CHAT_ID": "123",
    }
    return TelegramBotConfig.from_raw(raw, root=root, env=env)


def _build_service_in_closed_loop(
    tmp_path: Path,
    config: TelegramBotConfig,
    voice_config: VoiceConfig,
) -> TelegramBotService:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return TelegramBotService(config, hub_root=tmp_path, voice_config=voice_config)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_whisper_disclaimer_appended_for_transcripts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    voice_config = VoiceConfig.from_raw({"enabled": True, "provider": "openai_whisper"})
    service = _build_service_in_closed_loop(tmp_path, config, voice_config)

    prompt = "hello"
    updated = service._maybe_append_whisper_disclaimer(
        prompt, transcript_text="voice text"
    )

    assert updated == f"{prompt}\n\n{WHISPER_TRANSCRIPT_DISCLAIMER}"


def test_whisper_disclaimer_skipped_for_other_providers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    voice_config = VoiceConfig.from_raw({"enabled": True, "provider": "local"})
    service = _build_service_in_closed_loop(tmp_path, config, voice_config)

    prompt = "hello"
    updated = service._maybe_append_whisper_disclaimer(
        prompt, transcript_text="voice text"
    )

    assert updated == prompt


def test_whisper_disclaimer_not_duplicated(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    voice_config = VoiceConfig.from_raw({"enabled": True, "provider": "openai_whisper"})
    service = _build_service_in_closed_loop(tmp_path, config, voice_config)

    prompt = f"hello\n\n{WHISPER_TRANSCRIPT_DISCLAIMER}"
    updated = service._maybe_append_whisper_disclaimer(
        prompt, transcript_text="voice text"
    )

    assert updated == prompt
