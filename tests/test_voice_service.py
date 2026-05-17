import pytest

from codex_autorunner.voice import (
    TranscriptionEvent,
    VoiceConfig,
    VoiceService,
    VoiceServiceError,
)


class DummyStream:
    def __init__(self, text: str = "", error: str = ""):
        self._text = text
        self._error = error
        self.chunks = []

    def send_chunk(self, chunk):
        self.chunks.append(chunk.data)
        return []

    def flush_final(self):
        if self._error:
            return [TranscriptionEvent(text="", is_final=True, error=self._error)]
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


def test_voice_service_reports_missing_local_provider():
    cfg = VoiceConfig.from_raw(
        {"enabled": True, "provider": "local_whisper", "warn_on_remote_api": False},
        env={"TEST_ENV": "1"},
    )
    provider = DummyProvider(DummyStream(error="local_provider_unavailable"))
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    with pytest.raises(VoiceServiceError) as exc:
        service.transcribe(b"audio")
    assert "local whisper provider" in str(exc.value).lower()


def test_voice_service_reports_missing_mlx_provider_with_voice_mlx_hint():
    cfg = VoiceConfig.from_raw(
        {"enabled": True, "provider": "mlx_whisper", "warn_on_remote_api": False},
        env={"TEST_ENV": "1"},
    )
    provider = DummyProvider(DummyStream(error="local_provider_unavailable"))
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    with pytest.raises(VoiceServiceError) as exc:
        service.transcribe(b"audio")
    assert "voice-mlx" in str(exc.value)


def test_voice_service_reports_missing_local_runtime_dependency():
    cfg = VoiceConfig.from_raw(
        {"enabled": True, "provider": "mlx_whisper", "warn_on_remote_api": False},
        env={"TEST_ENV": "1"},
    )
    provider = DummyProvider(DummyStream(error="local_runtime_dependency_missing"))
    service = VoiceService(cfg, provider_resolver=lambda _: provider)

    with pytest.raises(VoiceServiceError) as exc:
        service.transcribe(b"audio")
    assert "ffmpeg" in str(exc.value).lower()


def test_voice_service_passes_env_to_provider_resolver():
    cfg = VoiceConfig.from_raw({"enabled": True, "warn_on_remote_api": False})
    provider = DummyProvider(DummyStream("ok"))
    captured: dict = {}

    def _resolver(config, logger=None, env=None):
        _ = config, logger
        captured["env"] = dict(env or {})
        return provider

    service = VoiceService(
        cfg,
        provider_resolver=_resolver,
        env={"OPENAI_API_KEY": "workspace-key"},
    )

    result = service.transcribe(b"audio bytes", client="web")
    assert result["text"] == "ok"
    assert captured["env"].get("OPENAI_API_KEY") == "workspace-key"


def test_voice_service_logs_error_at_startup_when_local_provider_missing(caplog):
    """VoiceService should log a clear ERROR at __init__ time when a local
    voice provider is configured but its Python package is not installed."""
    import logging
    import sys

    from codex_autorunner.voice.provider_catalog import (
        check_local_voice_provider_available,
    )

    # Block mlx_whisper to simulate a missing install.
    class _Blocker:
        def find_module(self, name, path=None):
            if name == "mlx_whisper":
                return self
            return None

        def load_module(self, name):
            raise ImportError("blocked by test")

    sys.meta_path.insert(0, _Blocker())
    # Remove any cached import so the blocker actually fires.
    sys.modules.pop("mlx_whisper", None)

    try:
        # Verify the check function detects the missing package.
        err = check_local_voice_provider_available("mlx_whisper")
        assert err is not None
        assert "voice-mlx" in err

        # Verify VoiceService logs the error during __init__.
        with caplog.at_level(logging.ERROR, logger="codex_autorunner.voice.service"):
            cfg = VoiceConfig.from_raw(
                {"enabled": True, "provider": "mlx_whisper"},
            )
            VoiceService(cfg)

        assert any(
            "Voice provider dependency check failed" in rec.message
            for rec in caplog.records
        )
    finally:
        # Clean up meta-path blocker so we don't break subsequent tests.
        sys.meta_path.pop(0)
