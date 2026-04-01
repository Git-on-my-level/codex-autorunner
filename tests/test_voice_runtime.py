from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.voice.config import VoiceConfig
from codex_autorunner.voice.provider import SpeechSessionMetadata
from codex_autorunner.voice.providers import build_speech_provider
from codex_autorunner.voice.service import VoicePermanentError, VoiceService


def test_openai_whisper_provider_respects_explicit_empty_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    provider = build_speech_provider({}, env={})

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        provider.start_stream(
            SpeechSessionMetadata(
                session_id="session-1",
                provider="openai_whisper",
                latency_mode="balanced",
            )
        )


def test_voice_service_transcribe_surfaces_missing_openai_api_key():
    config = VoiceConfig.from_raw(
        {"enabled": True, "provider": "openai_whisper"},
        env={},
    )

    def _resolver(config, *, logger=None, env=None):
        raise ValueError(
            "OpenAI Whisper provider requires API key env 'OPENAI_API_KEY' to be set."
        )

    service = VoiceService(config, provider_resolver=_resolver, env={})

    with pytest.raises(VoicePermanentError) as exc_info:
        service.transcribe(b"voice-bytes", client="discord")

    assert exc_info.value.reason == "missing_api_key"
    assert exc_info.value.user_message == (
        "Voice transcription failed: missing API key. "
        "Set OPENAI_API_KEY or install a local whisper provider."
    )


def test_voice_service_config_payload_uses_effective_fallback_provider():
    config = VoiceConfig.from_raw(
        {"enabled": True, "provider": "openai_whisper"},
        env={},
    )
    sentinel = SimpleNamespace(name="local_whisper")
    service = VoiceService(
        config,
        provider_resolver=lambda config, *, logger=None, env=None: sentinel,
        env={},
    )

    payload = service.config_payload()

    assert payload["provider"] == "openai_whisper"
    assert payload["effective_provider"] == "local_whisper"
    assert payload["has_api_key"] is True
    assert payload["api_key_env"] is None
