"""
Voice transcription and configuration routes.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from ....voice import VoiceService, VoiceServiceError

logger = logging.getLogger("codex_autorunner.routes.voice")


def build_voice_routes() -> APIRouter:
    """Build routes for voice transcription and config."""
    router = APIRouter()

    @router.get("/api/voice/config")
    def get_voice_config(request: Request):
        voice_service: Optional[VoiceService] = request.app.state.voice_service
        voice_config = request.app.state.voice_config
        missing_reason = getattr(request.app.state, "voice_missing_reason", None)
        if missing_reason:
            return {
                "enabled": False,
                "provider": voice_config.provider,
                "latency_mode": voice_config.latency_mode,
                "chunk_ms": voice_config.chunk_ms,
                "sample_rate": voice_config.sample_rate,
                "warn_on_remote_api": voice_config.warn_on_remote_api,
                "has_api_key": False,
                "push_to_talk": {
                    "max_ms": voice_config.push_to_talk.max_ms,
                    "silence_auto_stop_ms": voice_config.push_to_talk.silence_auto_stop_ms,
                    "min_hold_ms": voice_config.push_to_talk.min_hold_ms,
                },
                "missing_extra": missing_reason,
            }
        if voice_service is None:
            # Degrade gracefully: still return config to the UI even if service init failed.
            try:
                return VoiceService(
                    voice_config, logger=request.app.state.logger
                ).config_payload()
            except (ValueError, TypeError, OSError) as exc:
                logger.debug("Failed to create VoiceService for config: %s", exc)
                return {
                    "enabled": False,
                    "provider": voice_config.provider,
                    "latency_mode": voice_config.latency_mode,
                    "chunk_ms": voice_config.chunk_ms,
                    "sample_rate": voice_config.sample_rate,
                    "warn_on_remote_api": voice_config.warn_on_remote_api,
                    "has_api_key": False,
                    "push_to_talk": {
                        "max_ms": voice_config.push_to_talk.max_ms,
                        "silence_auto_stop_ms": voice_config.push_to_talk.silence_auto_stop_ms,
                        "min_hold_ms": voice_config.push_to_talk.min_hold_ms,
                    },
                }
        return voice_service.config_payload()

    @router.post("/api/voice/transcribe")
    async def transcribe_voice(
        request: Request,
        file: Optional[UploadFile] = File(None),
        language: Optional[str] = None,
    ):
        voice_service: Optional[VoiceService] = request.app.state.voice_service
        voice_config = request.app.state.voice_config
        missing_reason = getattr(request.app.state, "voice_missing_reason", None)
        if missing_reason:
            raise HTTPException(status_code=503, detail=missing_reason)
        if not voice_service or not voice_config.enabled:
            raise HTTPException(status_code=400, detail="Voice is disabled")

        filename: Optional[str] = None
        content_type: Optional[str] = None
        if file is not None:
            filename = file.filename
            content_type = file.content_type
            try:
                audio_bytes = await file.read()
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail="Unable to read audio upload"
                ) from exc
        else:
            audio_bytes = await request.body()
        try:
            result = await asyncio.to_thread(
                voice_service.transcribe,
                audio_bytes,
                client="web",
                user_agent=request.headers.get("user-agent"),
                language=language,
                filename=filename,
                content_type=content_type,
            )
        except VoiceServiceError as exc:
            if exc.reason == "unauthorized":
                status = 401
            elif exc.reason == "forbidden":
                status = 403
            elif exc.reason == "audio_too_large":
                status = 413
            elif exc.reason == "rate_limited":
                status = 429
            else:
                status = (
                    400
                    if exc.reason in ("disabled", "empty_audio", "invalid_audio")
                    else 502
                )
            raise HTTPException(status_code=status, detail=exc.detail) from exc
        return {"status": "ok", **result}

    return router
