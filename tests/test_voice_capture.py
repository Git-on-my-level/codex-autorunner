from __future__ import annotations

from codex_autorunner.voice import (
    AudioChunk,
    CaptureCallbacks,
    CaptureState,
    PushToTalkCapture,
    SpeechProvider,
    SpeechSessionMetadata,
    TranscriptionEvent,
    TranscriptionStream,
    VoiceConfig,
)


class FakeClock:
    def __init__(self) -> None:
        self._now = 0.0

    def advance(self, ms: int) -> None:
        self._now += ms / 1000.0

    def now(self) -> float:
        return self._now


class FakeStream(TranscriptionStream):
    def __init__(
        self,
        *,
        fail_on_chunk: bool = False,
        fail_flush: bool = False,
        chunk_events: list[TranscriptionEvent] | None = None,
        final_text: str = "final",
    ) -> None:
        self.chunks: list[AudioChunk] = []
        self.fail_on_chunk = fail_on_chunk
        self.fail_flush = fail_flush
        self.chunk_events = chunk_events or []
        self.final_text = final_text
        self.aborted_reason: str | None = None

    def send_chunk(self, chunk: AudioChunk):
        self.chunks.append(chunk)
        if self.fail_on_chunk:
            self.fail_on_chunk = False
            raise RuntimeError("chunk failure")
        return list(self.chunk_events)

    def flush_final(self):
        if self.fail_flush:
            self.fail_flush = False
            raise RuntimeError("flush failure")
        return [TranscriptionEvent(text=self.final_text, is_final=True)]

    def abort(self, reason=None) -> None:
        self.aborted_reason = reason or ""


class FakeProvider(SpeechProvider):
    name = "fake"
    supports_streaming = True

    def __init__(self, streams: list[FakeStream]) -> None:
        self.streams = list(streams)
        self.started = 0

    def start_stream(self, session: SpeechSessionMetadata) -> TranscriptionStream:
        self.started += 1
        if not self.streams:
            raise RuntimeError("no streams left")
        return self.streams.pop(0)


def test_capture_starts_without_opt_in():
    config = VoiceConfig.from_raw({"enabled": True, "warn_on_remote_api": True})
    provider = FakeProvider([FakeStream()])
    errors: list[str] = []
    finals: list[str] = []
    capture = PushToTalkCapture(
        provider,
        config,
        callbacks=CaptureCallbacks(on_error=errors.append, on_final=finals.append),
        permission_requester=lambda: True,
        now_fn=lambda: 0.0,
    )

    capture.begin_capture()
    capture.handle_chunk(b"\x00\x01")
    capture.end_capture()

    assert finals == ["final"]
    assert capture.state == CaptureState.IDLE
    assert provider.started == 1


def test_capture_retries_and_stops_on_silence():
    clock = FakeClock()
    config = VoiceConfig.from_raw(
        {
            "enabled": True,
            "warn_on_remote_api": False,
            "push_to_talk": {"max_ms": 1_200, "silence_auto_stop_ms": 400, "min_hold_ms": 50},
            "chunk_ms": 100,
        }
    )
    streams = [
        FakeStream(fail_on_chunk=True, chunk_events=[]),
        FakeStream(final_text="ok"),
    ]
    provider = FakeProvider(streams)
    warnings: list[str] = []
    finals: list[str] = []
    capture = PushToTalkCapture(
        provider,
        config,
        callbacks=CaptureCallbacks(on_warning=warnings.append, on_final=finals.append),
        permission_requester=lambda: True,
        now_fn=clock.now,
        max_retries=1,
    )

    capture.begin_capture()
    capture.handle_chunk(b"\x00\x00")
    assert warnings and "provider_error_retry" in warnings[-1]
    assert provider.started == 2  # restarted after failure

    clock.advance(500)
    capture.tick()  # silence auto-stops and flushes

    assert finals == ["ok"]
    assert capture.state == CaptureState.IDLE
