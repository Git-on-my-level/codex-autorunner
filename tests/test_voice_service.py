from codex_autorunner.voice import (
    TranscriptionEvent,
    VoiceConfig,
    VoiceService,
    VoiceServiceError,
)


class DummyStream:
    def __init__(self, text: str = ""):
        self._text = text
        self.chunks = []

    def send_chunk(self, chunk):
        self.chunks.append(chunk.data)
        return []

    def flush_final(self):
        final_text = self._text or b"".join(self.chunks).decode(
            "utf-8", errors="ignore"
        )
        return [TranscriptionEvent(text=final_text, is_final=True)]

    def abort(self, reason=None):
        pass


class DummyProvider:
    name = "dummy"
    supports_streaming = True

    def __init__(self, stream: DummyStream):
        self._stream = stream
        self.last_session = None

    def start_stream(self, session):
        self.last_session = session
        return self._stream


def test_voice_service_transcribes_bytes():
    cfg = VoiceConfig.from_raw({"enabled": True, "warn_on_remote_api": False})
    stream = DummyStream()
    provider = DummyProvider(stream)
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    result = service.transcribe(b"hello world", client="web")

    assert result["text"] == "hello world"
    assert result.get("warnings") == []


def test_voice_service_does_not_require_opt_in_when_warn_enabled():
    cfg = VoiceConfig.from_raw({"enabled": True, "warn_on_remote_api": True})
    provider = DummyProvider(DummyStream("ignored"))
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    result = service.transcribe(b"audio bytes", client="web")
    assert result["text"] == "ignored"


def test_voice_service_passes_filename_and_content_type_into_session():
    cfg = VoiceConfig.from_raw({"enabled": True, "warn_on_remote_api": False})
    provider = DummyProvider(DummyStream("ok"))
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    service.transcribe(
        b"audio bytes",
        client="web",
        filename="voice.webm",
        content_type="audio/webm;codecs=opus",
    )

    assert provider.last_session is not None
    assert provider.last_session.filename == "voice.webm"
    assert provider.last_session.content_type == "audio/webm;codecs=opus"
