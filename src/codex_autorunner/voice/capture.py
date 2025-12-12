from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol

from .config import VoiceConfig
from .provider import (
    AudioChunk,
    SpeechProvider,
    SpeechSessionMetadata,
    TranscriptionEvent,
)


class CaptureState(str, Enum):
    IDLE = "idle"
    AWAITING_PERMISSION = "awaiting_permission"
    RECORDING = "recording"
    STREAMING = "streaming"
    FINALIZING = "finalizing"
    ERROR = "error"


@dataclasses.dataclass
class CaptureCallbacks:
    on_state: Optional[Callable[[CaptureState], None]] = None
    on_partial: Optional[Callable[[str], None]] = None
    on_final: Optional[Callable[[str], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_warning: Optional[Callable[[str], None]] = None


class VoiceCaptureSession(Protocol):
    """
    Push-to-talk lifecycle contract shared by web and TUI surfaces.

    Implementations should be thin wrappers around platform-specific recorders.
    """

    def request_permission(self) -> None:
        """Prompt for microphone permission if needed."""
        ...

    def begin_capture(self) -> None:
        """Transition to recording and prepare buffers."""
        ...

    def handle_chunk(self, data: bytes) -> None:
        """Accept raw PCM/encoded chunk and forward to the provider stream."""
        ...

    def end_capture(self, reason: Optional[str] = None) -> None:
        """Stop recording and flush final transcription."""
        ...

    def fail(self, reason: str) -> None:
        """Force-fail the session and surface the reason to the UI."""
        ...


class PushToTalkCapture(VoiceCaptureSession):
    """
    Cross-platform push-to-talk controller that sits between UI recorders and a SpeechProvider.

    This keeps raw audio in-memory only and exposes explicit states so both TUI and web can
    render consistent UX.
    """

    def __init__(
        self,
        provider: SpeechProvider,
        config: VoiceConfig,
        callbacks: Optional[CaptureCallbacks] = None,
        permission_requester: Optional[Callable[[], bool]] = None,
        client: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        now_fn: Callable[[], float] = time.monotonic,
        max_retries: int = 1,
        session_builder: Optional[Callable[[], SpeechSessionMetadata]] = None,
    ) -> None:
        self._provider = provider
        self._config = config
        self._callbacks = callbacks or CaptureCallbacks()
        self._permission_requester = permission_requester or (lambda: True)
        self._client = client
        self._logger = logger or logging.getLogger(__name__)
        self._now = now_fn
        self._max_retries = max_retries
        self._session_builder = session_builder

        self._state: CaptureState = CaptureState.IDLE
        self._permission_granted = False
        self._stream = None
        self._retry_attempts = 0
        self._chunks: list[AudioChunk] = []
        self._sequence = 0
        self._started_at: Optional[float] = None
        self._last_chunk_at: Optional[float] = None

    @property
    def state(self) -> CaptureState:
        return self._state

    def request_permission(self) -> None:
        if self._state not in (CaptureState.IDLE, CaptureState.ERROR):
            return
        self._emit_state(CaptureState.AWAITING_PERMISSION)
        try:
            granted = bool(self._permission_requester())
        except Exception as exc:
            self.fail("permission_error")
            self._logger.error(
                "Microphone permission request failed: %s", exc, exc_info=False
            )
            return

        if not granted:
            self.fail("permission_denied")
            return

        self._permission_granted = True
        self._emit_state(CaptureState.IDLE)

    def begin_capture(self) -> None:
        if not self._permission_granted:
            self.request_permission()
            if not self._permission_granted:
                return

        if self._state in (
            CaptureState.RECORDING,
            CaptureState.STREAMING,
            CaptureState.FINALIZING,
        ):
            self.fail("already_recording")
            return

        try:
            self._stream = self._provider.start_stream(self._build_session_metadata())
        except Exception as exc:
            self.fail("provider_error")
            self._logger.error(
                "Failed to start transcription stream: %s", exc, exc_info=False
            )
            return

        now = self._now()
        self._started_at = now
        self._last_chunk_at = now
        self._sequence = 0
        self._retry_attempts = 0
        self._chunks = []
        self._emit_state(CaptureState.RECORDING)

    def handle_chunk(self, data: bytes) -> None:
        if self._stream is None:
            self.fail("not_started")
            return
        if self._state not in (CaptureState.RECORDING, CaptureState.STREAMING):
            return

        chunk = AudioChunk(
            data=data,
            sample_rate=self._config.sample_rate,
            start_ms=self._sequence * self._config.chunk_ms,
            end_ms=(self._sequence + 1) * self._config.chunk_ms,
            sequence=self._sequence,
        )
        self._chunks.append(chunk)
        self._sequence += 1
        self._last_chunk_at = self._now()

        try:
            events = self._stream.send_chunk(chunk)
            self._emit_state(CaptureState.STREAMING)
            self._handle_events(events)
        except Exception as exc:
            self._logger.warning(
                "Transcription chunk failed; will retry if allowed: %s",
                exc,
                exc_info=False,
            )
            if not self._fail_with_retry("provider_error"):
                return

        self._check_timeouts()

    def tick(self) -> None:
        """
        Allows hosts to poll for silence/timeout without spawning timers.
        Call from UI loops to auto-stop after silence or max duration.
        """
        self._check_timeouts()

    def end_capture(self, reason: Optional[str] = None) -> None:
        if self._stream is None:
            self._emit_state(CaptureState.IDLE)
            return

        while True:
            self._emit_state(CaptureState.FINALIZING)
            prior_retries = self._retry_attempts
            try:
                events = self._stream.flush_final()
                self._handle_events(events)
            except Exception as exc:
                self._logger.error(
                    "Final transcription flush failed: %s", exc, exc_info=False
                )
                if self._fail_with_retry("provider_error"):
                    continue
                return

            # If _handle_events triggered a retry due to an error event, we restarted the
            # stream and replayed chunks. We must attempt the final flush again on the
            # restarted stream, otherwise transcription will never be produced.
            if self._state == CaptureState.ERROR:
                return
            if self._retry_attempts > prior_retries:
                continue
            break

        self._reset()
        self._emit_state(CaptureState.IDLE)

    def fail(self, reason: str) -> None:
        if self._stream is not None:
            try:
                self._stream.abort(reason)
            except Exception:
                # Abort failures should not mask the root cause.
                pass
        self._reset()
        self._emit_error(reason)
        self._emit_state(CaptureState.ERROR)

    def _build_session_metadata(self) -> SpeechSessionMetadata:
        if self._session_builder:
            return self._session_builder()
        return SpeechSessionMetadata(
            session_id=str(uuid.uuid4()),
            provider=self._provider.name,
            latency_mode=self._config.latency_mode,
            client=self._client,
        )

    def _handle_events(self, events: Iterable[TranscriptionEvent]) -> None:
        for event in events:
            if event.error:
                if not self._fail_with_retry(event.error):
                    return
                continue
            if event.is_final:
                if event.text:
                    self._emit_final(event.text)
            else:
                if event.text:
                    self._emit_partial(event.text)

    def _emit_state(self, state: CaptureState) -> None:
        if state == self._state:
            return
        self._state = state
        if self._callbacks.on_state:
            self._callbacks.on_state(state)

    def _emit_partial(self, text: str) -> None:
        if self._callbacks.on_partial:
            self._callbacks.on_partial(text)

    def _emit_final(self, text: str) -> None:
        if self._callbacks.on_final:
            self._callbacks.on_final(text)

    def _emit_error(self, reason: str) -> None:
        if self._callbacks.on_error:
            self._callbacks.on_error(reason)

    def _emit_warning(self, message: str) -> None:
        if self._callbacks.on_warning:
            self._callbacks.on_warning(message)

    def _check_timeouts(self) -> None:
        if self._state not in (CaptureState.RECORDING, CaptureState.STREAMING):
            return
        now = self._now()
        if (
            self._started_at is not None
            and (now - self._started_at) * 1000 >= self._config.push_to_talk.max_ms
        ):
            self.end_capture("max_duration")
            return
        if (
            self._last_chunk_at is not None
            and (now - self._last_chunk_at) * 1000
            >= self._config.push_to_talk.silence_auto_stop_ms
        ):
            self.end_capture("silence")

    def _fail_with_retry(self, reason: str) -> bool:
        if reason in (
            "unauthorized",
            "forbidden",
            "invalid_audio",
            "audio_too_large",
            "rate_limited",
        ):
            self.fail(reason)
            return False
        if self._retry_attempts >= self._max_retries:
            self.fail(reason)
            return False

        self._retry_attempts += 1
        self._emit_warning(f"{reason}_retry")
        try:
            self._restart_stream()
            return True
        except Exception as exc:
            self._logger.error(
                "Retrying transcription stream failed: %s", exc, exc_info=False
            )
            self.fail(reason)
            return False

    def _restart_stream(self) -> None:
        self._stream = self._provider.start_stream(self._build_session_metadata())
        replayed_state = (
            CaptureState.RECORDING if not self._chunks else CaptureState.STREAMING
        )
        for chunk in self._chunks:
            events = self._stream.send_chunk(chunk)
            self._handle_events(events)
        self._emit_state(replayed_state)
        self._last_chunk_at = self._now()

    def _reset(self) -> None:
        self._stream = None
        self._chunks = []
        self._sequence = 0
        self._started_at = None
        self._last_chunk_at = None
