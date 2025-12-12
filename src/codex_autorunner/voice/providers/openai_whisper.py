from __future__ import annotations

import dataclasses
import logging
import os
import time
from io import BytesIO
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from ..provider import AudioChunk, SpeechProvider, SpeechSessionMetadata, TranscriptionEvent, TranscriptionStream


RequestFn = Callable[[bytes, Mapping[str, Any]], Dict[str, Any]]


@dataclasses.dataclass
class OpenAIWhisperSettings:
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "whisper-1"
    base_url: Optional[str] = None
    temperature: float = 0.0
    language: Optional[str] = None
    redact_request: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "OpenAIWhisperSettings":
        return cls(
            api_key_env=str(raw.get("api_key_env", "OPENAI_API_KEY")),
            model=str(raw.get("model", "whisper-1")),
            base_url=raw.get("base_url"),
            temperature=float(raw.get("temperature", 0.0)),
            language=raw.get("language"),
            redact_request=bool(raw.get("redact_request", True)),
        )


class OpenAIWhisperProvider(SpeechProvider):
    """
    Whisper transcription provider behind the SpeechProvider abstraction.

    This keeps raw audio in-memory only and redacts request metadata by default.
    """

    name = "openai_whisper"
    supports_streaming = False  # OpenAI Whisper is request/response; we buffer chunks locally.

    def __init__(
        self,
        settings: OpenAIWhisperSettings,
        env: Optional[Mapping[str, str]] = None,
        warn_on_remote_api: bool = True,
        logger: Optional[logging.Logger] = None,
        request_fn: Optional[RequestFn] = None,
    ) -> None:
        self._settings = settings
        self._env = env or os.environ
        self._warn_on_remote_api = warn_on_remote_api
        self._logger = logger or logging.getLogger(__name__)
        self._request_fn: RequestFn = request_fn or self._default_request

    def start_stream(self, session: SpeechSessionMetadata) -> TranscriptionStream:
        api_key = self._env.get(self._settings.api_key_env)
        if api_key:
            # Defensive normalization: .env / launchd / shells sometimes introduce
            # trailing newlines or quoting that can yield 401s.
            api_key = api_key.strip().strip('"').strip("'").strip("`").strip()
        if not api_key:
            raise ValueError(
                f"OpenAI Whisper provider requires API key env '{self._settings.api_key_env}' to be set"
            )
        return _OpenAIWhisperStream(
            api_key=api_key,
            settings=self._settings,
            session=session,
            warn_on_remote_api=self._warn_on_remote_api,
            logger=self._logger,
            request_fn=self._request_fn,
        )

    def _default_request(self, audio_bytes: bytes, payload: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            import httpx
        except Exception as exc:  # pragma: no cover - import failure path is simple
            raise RuntimeError(
                "httpx is required for the OpenAI Whisper provider; install via `pip install httpx`"
            ) from exc

        headers = {"Authorization": f"Bearer {payload['api_key']}"}
        url = f"{payload['base_url'].rstrip('/')}/v1/audio/transcriptions"
        data: Dict[str, Any] = {"model": payload["model"], "temperature": payload["temperature"]}
        if payload.get("language"):
            data["language"] = payload["language"]
        files = {"file": ("audio.webm", BytesIO(audio_bytes), "application/octet-stream")}
        response = httpx.post(url, headers=headers, data=data, files=files, timeout=30)
        response.raise_for_status()
        return response.json()


class _OpenAIWhisperStream(TranscriptionStream):
    def __init__(
        self,
        api_key: str,
        settings: OpenAIWhisperSettings,
        session: SpeechSessionMetadata,
        warn_on_remote_api: bool,
        logger: logging.Logger,
        request_fn: RequestFn,
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self._session = session
        self._warn_on_remote_api = warn_on_remote_api
        self._logger = logger
        self._request_fn = request_fn
        self._started_at = time.monotonic()
        self._chunks: list[bytes] = []
        self._aborted = False

    def send_chunk(self, chunk: AudioChunk) -> Iterable[TranscriptionEvent]:
        # Only retain raw bytes in-memory until the final request to avoid persistence.
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
        if self._warn_on_remote_api:
            self._logger.warning(
                "Sending audio to OpenAI Whisper (%s); audio bytes are not logged or persisted.",
                self._settings.model,
            )

        payload = self._build_payload()
        status_code: Optional[int] = None
        try:
            started = time.monotonic()
            result = self._request_fn(audio_bytes, payload)
            latency_ms = int((time.monotonic() - started) * 1000)
            text = (result or {}).get("text", "") if isinstance(result, Mapping) else ""
            return [TranscriptionEvent(text=text, is_final=True, latency_ms=latency_ms)]
        except Exception as exc:
            try:
                import httpx  # type: ignore

                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    status_code = exc.response.status_code
            except Exception:
                status_code = None
            self._logger.error("OpenAI Whisper transcription failed: %s", exc, exc_info=False)
            # Avoid retry loops for credential errors; surface explicit reasons.
            if status_code == 401:
                return [TranscriptionEvent(text="", is_final=True, error="unauthorized")]
            if status_code == 403:
                return [TranscriptionEvent(text="", is_final=True, error="forbidden")]
            return [TranscriptionEvent(text="", is_final=True, error="provider_error")]
        finally:
            # Release buffered bytes to avoid accidental reuse.
            self._chunks = []

    def abort(self, reason: Optional[str] = None) -> None:
        self._aborted = True
        self._chunks = []
        if reason:
            self._logger.info("OpenAI Whisper stream aborted: %s", reason)

    def _build_payload(self) -> Dict[str, Any]:
        base_url = self._settings.base_url or "https://api.openai.com"
        payload = {
            "api_key": self._api_key,
            "base_url": base_url,
            "model": self._settings.model,
            "temperature": self._settings.temperature,
            "language": self._settings.language or self._session.language,
        }
        if not self._settings.redact_request:
            payload.update(
                {
                    "client": self._session.client,
                    "session_id": self._session.session_id,
                }
            )
        return payload


def build_speech_provider(
    config: Mapping[str, Any],
    warn_on_remote_api: bool = True,
    env: Optional[Mapping[str, str]] = None,
    logger: Optional[logging.Logger] = None,
) -> OpenAIWhisperProvider:
    """
    Factory used by voice resolver to construct the Whisper provider from config mappings.
    """
    settings = OpenAIWhisperSettings.from_mapping(config)
    return OpenAIWhisperProvider(
        settings=settings,
        env=env,
        warn_on_remote_api=warn_on_remote_api,
        logger=logger,
    )
