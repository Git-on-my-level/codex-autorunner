from codex_autorunner.voice import (
    AudioChunk,
    OpenAIWhisperProvider,
    OpenAIWhisperSettings,
    SpeechSessionMetadata,
    VoiceConfig,
    resolve_speech_provider,
)


def test_openai_whisper_respects_env_and_redaction():
    settings = OpenAIWhisperSettings(
        api_key_env="CUSTOM_OPENAI_KEY", temperature=0.2, language="en"
    )
    captured = {}

    def fake_request(audio_bytes, payload):
        nonlocal captured
        captured = dict(payload)
        return {"text": "hello world"}

    provider = OpenAIWhisperProvider(
        settings=settings,
        env={"CUSTOM_OPENAI_KEY": "secret"},
        warn_on_remote_api=False,
        request_fn=fake_request,
    )
    stream = provider.start_stream(
        SpeechSessionMetadata(
            session_id="abc",
            provider="openai_whisper",
            latency_mode="balanced",
            language="en",
            client="web",
        )
    )
    stream.send_chunk(
        AudioChunk(
            data=b"\x00\x01", sample_rate=16_000, start_ms=0, end_ms=100, sequence=0
        )
    )
    events = list(stream.flush_final())

    assert events and events[0].text == "hello world"
    assert captured["api_key"] == "secret"
    assert captured["model"] == "whisper-1"
    assert "session_id" not in captured  # redacted by default
    assert "client" not in captured


def test_openai_whisper_can_include_session_when_not_redacted():
    settings = OpenAIWhisperSettings(api_key_env="OPENAI_API_KEY", redact_request=False)
    sent_payloads = []

    def fake_request(audio_bytes, payload):
        sent_payloads.append(dict(payload))
        return {"text": "ok"}

    provider = OpenAIWhisperProvider(
        settings=settings,
        env={"OPENAI_API_KEY": "token"},
        warn_on_remote_api=False,
        request_fn=fake_request,
    )
    stream = provider.start_stream(
        SpeechSessionMetadata(
            session_id="session-1",
            provider="openai_whisper",
            latency_mode="balanced",
            client="tui",
        )
    )
    stream.send_chunk(
        AudioChunk(data=b"\x00", sample_rate=16_000, start_ms=0, end_ms=10, sequence=0)
    )
    list(stream.flush_final())

    assert sent_payloads
    payload = sent_payloads[0]
    assert payload["session_id"] == "session-1"
    assert payload["client"] == "tui"


def test_openai_whisper_passes_filename():
    settings = OpenAIWhisperSettings()
    captured = {}

    def fake_request(audio_bytes, payload):
        nonlocal captured
        captured = dict(payload)
        return {"text": "ok"}

    provider = OpenAIWhisperProvider(
        settings=settings,
        env={"OPENAI_API_KEY": "token"},
        warn_on_remote_api=False,
        request_fn=fake_request,
    )
    stream = provider.start_stream(
        SpeechSessionMetadata(
            session_id="session-1",
            provider="openai_whisper",
            latency_mode="balanced",
            filename="voice.ogg",
        )
    )
    stream.send_chunk(
        AudioChunk(data=b"\x00", sample_rate=16_000, start_ms=0, end_ms=10, sequence=0)
    )
    list(stream.flush_final())

    assert captured["filename"] == "voice.ogg"


def test_resolve_speech_provider_builds_openai():
    config = VoiceConfig.from_raw(
        {
            "enabled": True,
            "provider": "openai_whisper",
            "providers": {"openai_whisper": {"model": "whisper-1"}},
            "warn_on_remote_api": False,
        }
    )
    provider = resolve_speech_provider(
        voice_config=config, env={"OPENAI_API_KEY": "token"}, logger=None
    )
    assert isinstance(provider, OpenAIWhisperProvider)
