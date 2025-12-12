from __future__ import annotations

import pytest

from codex_autorunner.voice import (
    PushToTalkCapture,
    VoiceConfig,
    resolve_speech_provider,
)


class _RecorderStream:
    def __init__(self):
        self.chunks = []

    def send_chunk(self, chunk):
        self.chunks.append(chunk)
        return []

    def flush_final(self):
        return []

    def abort(self, reason=None):
        pass


class _InspectingProvider:
    name = "openai_whisper"
    supports_streaming = True

    def __init__(self):
        self.last_session = None
        self.stream = _RecorderStream()

    def start_stream(self, session):
        self.last_session = session
        return self.stream


def test_voice_config_env_overrides_and_provider_defaults():
    raw = {
        "enabled": False,
        "provider": "openai_whisper",
        "latency_mode": "quality",
        "chunk_ms": 800,
        "sample_rate": 44_100,
        "push_to_talk": {"max_ms": 10_000},
        "providers": {"openai_whisper": {"base_url": "https://api.example.com"}},
    }
    env = {
        "CODEX_AUTORUNNER_VOICE_ENABLED": "1",
        "CODEX_AUTORUNNER_VOICE_PROVIDER": "custom",
        "CODEX_AUTORUNNER_VOICE_LATENCY": "realtime",
        "CODEX_AUTORUNNER_VOICE_CHUNK_MS": "450",
        "CODEX_AUTORUNNER_VOICE_SAMPLE_RATE": "22050",
        "CODEX_AUTORUNNER_VOICE_MAX_MS": "9000",
        "CODEX_AUTORUNNER_VOICE_SILENCE_MS": "750",
        "CODEX_AUTORUNNER_VOICE_MIN_HOLD_MS": "80",
    }

    cfg = VoiceConfig.from_raw(raw, env=env)

    assert cfg.enabled is True
    assert cfg.provider == "custom"
    assert cfg.latency_mode == "realtime"
    assert cfg.chunk_ms == 450
    assert cfg.sample_rate == 22_050
    assert cfg.push_to_talk.max_ms == 9000
    assert cfg.push_to_talk.silence_auto_stop_ms == 750
    assert cfg.push_to_talk.min_hold_ms == 80

    whisper_cfg = cfg.providers["openai_whisper"]
    assert whisper_cfg["model"] == "whisper-1"  # default preserved
    assert whisper_cfg["base_url"] == "https://api.example.com"  # override applied


def test_resolve_provider_requires_enabled_and_known_provider():
    # Avoid auto-enabling when local dev env has an API key loaded.
    disabled = VoiceConfig.from_raw(
        {"enabled": False}, env={"CODEX_AUTORUNNER_VOICE_ENABLED": "0"}
    )
    with pytest.raises(ValueError):
        resolve_speech_provider(disabled)

    unknown = VoiceConfig.from_raw({"enabled": True, "provider": "does_not_exist"})
    with pytest.raises(ValueError):
        resolve_speech_provider(unknown)


def test_capture_uses_latency_mode_and_chunk_size():
    provider = _InspectingProvider()
    cfg = VoiceConfig.from_raw(
        {
            "enabled": True,
            "latency_mode": "quality",
            "chunk_ms": 900,
            "warn_on_remote_api": False,
        }
    )
    capture = PushToTalkCapture(
        provider,
        cfg,
        permission_requester=lambda: True,
    )
    capture.begin_capture()
    capture.handle_chunk(b"\x00\x01")

    assert provider.last_session is not None
    assert provider.last_session.latency_mode == "quality"
    assert provider.stream.chunks and provider.stream.chunks[0].start_ms == 0
    assert provider.stream.chunks[0].end_ms == 900
