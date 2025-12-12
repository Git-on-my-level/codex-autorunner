from __future__ import annotations

import logging
import os
import uuid
from typing import Callable, Optional

from .capture import CaptureCallbacks, CaptureState, PushToTalkCapture
from .config import VoiceConfig
from .provider import SpeechSessionMetadata
from .resolver import resolve_speech_provider


class VoiceServiceError(Exception):
    """Raised when voice transcription fails at the service boundary."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class VoiceService:
    """
    Thin wrapper that wires the shared PushToTalkCapture into HTTP handlers.
    This keeps raw audio in-memory only and centralizes provider wiring/error mapping.
    """

    def __init__(
        self,
        config: VoiceConfig,
        logger: Optional[logging.Logger] = None,
        provider_resolver: Callable[[VoiceConfig], object] = resolve_speech_provider,
        provider: Optional[object] = None,
        env: Optional[dict] = None,
    ) -> None:
        self.config = config
        self._logger = logger or logging.getLogger(__name__)
        self._provider_resolver = provider_resolver
        self._provider = provider
        self._env = env if env is not None else os.environ

    def config_payload(self) -> dict:
        """Expose safe config fields to the UI."""
        # Check if API key is configured for status display
        provider_cfg = self.config.providers.get(
            self.config.provider or "openai_whisper", {}
        )
        api_key_env = provider_cfg.get("api_key_env", "OPENAI_API_KEY")
        has_api_key = bool(self._env.get(api_key_env))

        return {
            "enabled": self.config.enabled,
            "provider": self.config.provider,
            "latency_mode": self.config.latency_mode,
            "chunk_ms": self.config.chunk_ms,
            "sample_rate": self.config.sample_rate,
            "warn_on_remote_api": self.config.warn_on_remote_api,
            "has_api_key": has_api_key,
            "api_key_env": api_key_env,
            "push_to_talk": {
                "max_ms": self.config.push_to_talk.max_ms,
                "silence_auto_stop_ms": self.config.push_to_talk.silence_auto_stop_ms,
                "min_hold_ms": self.config.push_to_talk.min_hold_ms,
            },
        }

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        client: str = "web",
        user_agent: Optional[str] = None,
        language: Optional[str] = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> dict:
        if not self.config.enabled:
            raise VoiceServiceError("disabled", "Voice is disabled")
        if not audio_bytes:
            raise VoiceServiceError("empty_audio", "No audio received")

        provider = self._resolve_provider()
        buffer = _TranscriptionBuffer()
        capture = PushToTalkCapture(
            provider=provider,
            config=self.config,
            callbacks=buffer.callbacks,
            permission_requester=lambda: True,
            client=client,
            logger=self._logger,
            session_builder=lambda: self._build_session_metadata(
                provider_name=provider.name,
                language=language,
                client=client,
                user_agent=user_agent,
                filename=filename,
                content_type=content_type,
            ),
        )

        capture.begin_capture()
        if capture.state == CaptureState.ERROR:
            reason = buffer.error_reason or "capture_failed"
            raise VoiceServiceError(reason, reason.replace("_", " "))

        try:
            capture.handle_chunk(audio_bytes)
            capture.end_capture("client_stop")
        except Exception as exc:
            raise VoiceServiceError("provider_error", str(exc)) from exc

        if buffer.error_reason:
            if buffer.error_reason in ("unauthorized", "forbidden"):
                provider_cfg = self.config.providers.get(
                    self.config.provider or "openai_whisper", {}
                )
                api_key_env = provider_cfg.get("api_key_env", "OPENAI_API_KEY")
                raise VoiceServiceError(
                    buffer.error_reason,
                    f"OpenAI API key rejected ({buffer.error_reason}); check {api_key_env}",
                )
            if buffer.error_reason == "invalid_audio":
                meta = ""
                if filename or content_type:
                    meta = f" (file={filename or 'audio'}, type={content_type or 'unknown'})"
                raise VoiceServiceError(
                    "invalid_audio",
                    "OpenAI rejected the audio upload (bad request). "
                    f"Try re-recording or switching formats/browsers{meta}.",
                )
            if buffer.error_reason == "audio_too_large":
                raise VoiceServiceError(
                    "audio_too_large",
                    "Audio upload too large; record a shorter clip and try again.",
                )
            if buffer.error_reason == "rate_limited":
                raise VoiceServiceError(
                    "rate_limited",
                    "OpenAI rate limited the request; wait a moment and try again.",
                )
            raise VoiceServiceError(
                buffer.error_reason, buffer.error_reason.replace("_", " ")
            )

        transcript = buffer.final_text or buffer.partial_text or ""
        return {
            "text": transcript,
            "warnings": buffer.warnings,
        }

    def _resolve_provider(self):
        if self._provider is None:
            try:
                self._provider = self._provider_resolver(
                    self.config, logger=self._logger
                )
            except TypeError:
                self._provider = self._provider_resolver(self.config)
        return self._provider

    def _build_session_metadata(
        self,
        *,
        provider_name: str,
        language: Optional[str],
        client: Optional[str],
        user_agent: Optional[str],
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> SpeechSessionMetadata:
        return SpeechSessionMetadata(
            session_id=str(uuid.uuid4()),
            provider=provider_name,
            latency_mode=self.config.latency_mode,
            language=language,
            client=client,
            user_agent=user_agent,
            filename=filename,
            content_type=content_type,
        )


class _TranscriptionBuffer:
    def __init__(self) -> None:
        self.partial_text = ""
        self.final_text = ""
        self.warnings: list[str] = []
        self.error_reason: Optional[str] = None
        self.callbacks = CaptureCallbacks(
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_warning=self._on_warning,
            on_error=self._on_error,
        )

    def _on_partial(self, text: str) -> None:
        if text:
            self.partial_text = text

    def _on_final(self, text: str) -> None:
        if text:
            self.final_text = text

    def _on_warning(self, message: str) -> None:
        if message:
            self.warnings.append(message)

    def _on_error(self, reason: str) -> None:
        self.error_reason = reason
