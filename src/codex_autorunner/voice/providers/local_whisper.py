from __future__ import annotations

import dataclasses
import io
import logging
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, cast

from ..provider import (
    AudioChunk,
    SpeechProvider,
    SpeechSessionMetadata,
    TranscriptionEvent,
    TranscriptionStream,
)


class _WhisperModelLike(Protocol):
    def transcribe(
        self,
        audio: Any,
        language: Optional[str] = None,
        beam_size: int = 1,
        vad_filter: bool = True,
    ) -> tuple[Iterable[Any], Any]: ...


@dataclasses.dataclass
class LocalWhisperSettings:
    model: str = "tiny"
    device: str = "auto"
    compute_type: str = "default"
    cpu_threads: int = 0
    num_workers: int = 1
    download_root: Optional[str] = None
    local_files_only: bool = False
    beam_size: int = 1
    vad_filter: bool = True
    language: Optional[str] = None
    redact_request: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LocalWhisperSettings":
        return cls(
            model=str(raw.get("model", "tiny")),
            device=str(raw.get("device", "auto")),
            compute_type=str(raw.get("compute_type", "default")),
            cpu_threads=int(raw.get("cpu_threads", 0)),
            num_workers=int(raw.get("num_workers", 1)),
            download_root=(
                str(raw["download_root"]).strip()
                if raw.get("download_root") is not None
                else None
            ),
            local_files_only=bool(raw.get("local_files_only", False)),
            beam_size=max(1, int(raw.get("beam_size", 1))),
            vad_filter=bool(raw.get("vad_filter", True)),
            language=(
                str(raw["language"]).strip()
                if raw.get("language") is not None
                else None
            ),
            redact_request=bool(raw.get("redact_request", True)),
        )


class LocalWhisperProvider(SpeechProvider):
    """
    Local faster-whisper provider.

    Audio bytes stay in-memory and are passed to faster-whisper via BytesIO.
    """

    name = "local_whisper"
    supports_streaming = False

    def __init__(
        self,
        settings: LocalWhisperSettings,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._model_lock = threading.Lock()
        self._model: Optional[_WhisperModelLike] = None

    def start_stream(self, session: SpeechSessionMetadata) -> TranscriptionStream:
        return _LocalWhisperStream(
            provider=self,
            settings=self._settings,
            session=session,
            logger=self._logger,
        )

    def _get_model(self) -> _WhisperModelLike:
        with self._model_lock:
            if self._model is not None:
                return self._model

            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Local Whisper provider requires faster-whisper. "
                    "Install with: pip install 'codex-autorunner[voice-local]'"
                ) from exc

            self._model = cast(
                _WhisperModelLike,
                WhisperModel(
                    self._settings.model,
                    device=self._settings.device,
                    compute_type=self._settings.compute_type,
                    cpu_threads=self._settings.cpu_threads,
                    num_workers=self._settings.num_workers,
                    download_root=self._settings.download_root,
                    local_files_only=self._settings.local_files_only,
                ),
            )
            return self._model

    def _transcribe(
        self, audio_bytes: bytes, payload: Mapping[str, Any]
    ) -> Dict[str, Any]:
        model = self._get_model()
        audio_buffer = io.BytesIO(audio_bytes)
        segments, info = model.transcribe(
            audio_buffer,
            language=cast(Optional[str], payload.get("language")),
            beam_size=int(payload.get("beam_size", 1)),
            vad_filter=bool(payload.get("vad_filter", True)),
        )
        text_parts = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                text_parts.append(text)
        text = " ".join(text_parts).strip()
        language = getattr(info, "language", None)
        return {"text": text, "language": language}


class _LocalWhisperStream(TranscriptionStream):
    def __init__(
        self,
        *,
        provider: LocalWhisperProvider,
        settings: LocalWhisperSettings,
        session: SpeechSessionMetadata,
        logger: logging.Logger,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._session = session
        self._logger = logger
        self._chunks: list[bytes] = []
        self._aborted = False

    def send_chunk(self, chunk: AudioChunk) -> Iterable[TranscriptionEvent]:
        if self._aborted:
            return []
        self._chunks.append(chunk.data)
        return []

    def flush_final(self) -> Iterable[TranscriptionEvent]:
        if self._aborted:
            return []
        if not self._chunks:
            return []

        audio_bytes = b"".join(self._chunks)
        payload = self._build_payload()
        try:
            started = time.monotonic()
            result = self._provider._transcribe(audio_bytes, payload)
            latency_ms = int((time.monotonic() - started) * 1000)
            text = (result or {}).get("text", "") if isinstance(result, Mapping) else ""
            return [TranscriptionEvent(text=text, is_final=True, latency_ms=latency_ms)]
        except RuntimeError as exc:
            message = str(exc)
            if "requires faster-whisper" in message:
                self._logger.error("Local Whisper unavailable: %s", message)
                return [
                    TranscriptionEvent(
                        text="", is_final=True, error="local_provider_unavailable"
                    )
                ]
            self._logger.error("Local Whisper transcription failed: %s", exc)
            return [TranscriptionEvent(text="", is_final=True, error="provider_error")]
        except Exception as exc:
            self._logger.error("Local Whisper transcription failed: %s", exc)
            return [TranscriptionEvent(text="", is_final=True, error="provider_error")]
        finally:
            self._chunks = []

    def abort(self, reason: Optional[str] = None) -> None:
        self._aborted = True
        self._chunks = []
        if reason:
            self._logger.info("Local Whisper stream aborted: %s", reason)

    def _build_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._settings.model,
            "device": self._settings.device,
            "compute_type": self._settings.compute_type,
            "beam_size": self._settings.beam_size,
            "vad_filter": self._settings.vad_filter,
            "language": self._settings.language or self._session.language,
        }
        if not self._settings.redact_request:
            payload.update(
                {
                    "session_id": self._session.session_id,
                    "client": self._session.client,
                }
            )
        return payload


def build_local_whisper_provider(
    config: Mapping[str, Any],
    logger: Optional[logging.Logger] = None,
) -> LocalWhisperProvider:
    settings = LocalWhisperSettings.from_mapping(config)
    return LocalWhisperProvider(settings=settings, logger=logger)
