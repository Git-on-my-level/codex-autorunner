from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from ..provider import (
    AudioChunk,
    SpeechProvider,
    SpeechSessionMetadata,
    TranscriptionEvent,
    TranscriptionStream,
)

_MODEL_ALIAS_TO_REPO = {
    "tiny": "mlx-community/whisper-tiny",
    "tiny.en": "mlx-community/whisper-tiny.en",
    "base": "mlx-community/whisper-base",
    "base.en": "mlx-community/whisper-base.en",
    "small": "mlx-community/whisper-small",
    "small.en": "mlx-community/whisper-small.en",
    "medium": "mlx-community/whisper-medium",
    "medium.en": "mlx-community/whisper-medium.en",
    "large-v3": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-turbo",
}


@dataclasses.dataclass
class MlxWhisperSettings:
    model: str = "small"
    language: Optional[str] = None
    beam_size: int = 1
    temperature: float = 0.0
    condition_on_previous_text: bool = False
    word_timestamps: bool = False
    initial_prompt: Optional[str] = None
    redact_request: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MlxWhisperSettings":
        return cls(
            model=str(raw.get("model", "small")),
            language=(
                str(raw["language"]).strip()
                if raw.get("language") is not None
                else None
            ),
            beam_size=max(1, int(raw.get("beam_size", 1))),
            temperature=float(raw.get("temperature", 0.0)),
            condition_on_previous_text=bool(
                raw.get("condition_on_previous_text", False)
            ),
            word_timestamps=bool(raw.get("word_timestamps", False)),
            initial_prompt=(
                str(raw["initial_prompt"]).strip()
                if raw.get("initial_prompt") is not None
                else None
            ),
            redact_request=bool(raw.get("redact_request", True)),
        )


class MlxWhisperProvider(SpeechProvider):
    """
    Local Apple Silicon Whisper provider using mlx-whisper.

    Audio bytes are buffered in-memory and written to a temporary file only for
    decoding, then removed immediately after transcription.
    """

    name = "mlx_whisper"
    supports_streaming = False

    def __init__(
        self,
        settings: MlxWhisperSettings,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)

    def start_stream(self, session: SpeechSessionMetadata) -> TranscriptionStream:
        return _MlxWhisperStream(
            provider=self,
            settings=self._settings,
            session=session,
            logger=self._logger,
        )

    def _transcribe(
        self, audio_bytes: bytes, payload: Mapping[str, Any], file_suffix: str
    ) -> Dict[str, Any]:
        try:
            from mlx_whisper import transcribe
        except ImportError as exc:
            raise RuntimeError(
                "MLX Whisper provider requires mlx-whisper. "
                "Install with: pip install 'codex-autorunner[voice-mlx]'"
            ) from exc

        fd, temp_path = tempfile.mkstemp(prefix="car-voice-", suffix=file_suffix)
        try:
            with os.fdopen(fd, "wb") as temp_file:
                temp_file.write(audio_bytes)

            result = transcribe(
                temp_path,
                path_or_hf_repo=str(payload.get("path_or_hf_repo", "small")),
                language=payload.get("language"),
                temperature=float(payload.get("temperature", 0.0)),
                initial_prompt=payload.get("initial_prompt"),
                word_timestamps=bool(payload.get("word_timestamps", False)),
                condition_on_previous_text=bool(
                    payload.get("condition_on_previous_text", False)
                ),
                beam_size=int(payload.get("beam_size", 1)),
            )
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        if isinstance(result, Mapping):
            text = str(result.get("text", "")).strip()
            language = result.get("language")
            return {"text": text, "language": language}
        return {"text": "", "language": None}


class _MlxWhisperStream(TranscriptionStream):
    def __init__(
        self,
        *,
        provider: MlxWhisperProvider,
        settings: MlxWhisperSettings,
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
        file_suffix = self._session_file_suffix()
        try:
            started = time.monotonic()
            result = self._provider._transcribe(audio_bytes, payload, file_suffix)
            latency_ms = int((time.monotonic() - started) * 1000)
            text = (result or {}).get("text", "") if isinstance(result, Mapping) else ""
            return [TranscriptionEvent(text=text, is_final=True, latency_ms=latency_ms)]
        except RuntimeError as exc:
            message = str(exc)
            if "requires mlx-whisper" in message:
                self._logger.error("MLX Whisper unavailable: %s", message)
                return [
                    TranscriptionEvent(
                        text="", is_final=True, error="local_provider_unavailable"
                    )
                ]
            self._logger.error("MLX Whisper transcription failed: %s", exc)
            return [TranscriptionEvent(text="", is_final=True, error="provider_error")]
        except Exception as exc:
            self._logger.error("MLX Whisper transcription failed: %s", exc)
            return [TranscriptionEvent(text="", is_final=True, error="provider_error")]
        finally:
            self._chunks = []

    def abort(self, reason: Optional[str] = None) -> None:
        self._aborted = True
        self._chunks = []
        if reason:
            self._logger.info("MLX Whisper stream aborted: %s", reason)

    def _build_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "path_or_hf_repo": _resolve_mlx_model_path(self._settings.model),
            "beam_size": self._settings.beam_size,
            "temperature": self._settings.temperature,
            "condition_on_previous_text": self._settings.condition_on_previous_text,
            "word_timestamps": self._settings.word_timestamps,
            "initial_prompt": self._settings.initial_prompt,
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

    def _session_file_suffix(self) -> str:
        filename = (self._session.filename or "").strip()
        if filename:
            suffix = Path(filename).suffix
            if suffix:
                return suffix
        return ".webm"


def _resolve_mlx_model_path(model: str) -> str:
    candidate = (model or "").strip()
    if not candidate:
        return _MODEL_ALIAS_TO_REPO["small"]
    if candidate.startswith("mlx-community/"):
        return candidate
    if "/" in candidate:
        return candidate
    if Path(candidate).exists():
        return candidate

    normalized = candidate.lower()
    if normalized in _MODEL_ALIAS_TO_REPO:
        return _MODEL_ALIAS_TO_REPO[normalized]
    return candidate


def build_mlx_whisper_provider(
    config: Mapping[str, Any],
    logger: Optional[logging.Logger] = None,
) -> MlxWhisperProvider:
    settings = MlxWhisperSettings.from_mapping(config)
    return MlxWhisperProvider(settings=settings, logger=logger)
