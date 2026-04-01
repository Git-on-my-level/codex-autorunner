from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.surfaces.web.routes.voice import build_voice_routes
from codex_autorunner.voice.config import VoiceConfig
from codex_autorunner.voice.service import VoicePermanentError


def _build_app(*, voice_service) -> FastAPI:
    app = FastAPI()
    app.include_router(build_voice_routes())
    app.state.voice_service = voice_service
    app.state.voice_config = VoiceConfig.from_raw(
        {"enabled": True, "provider": "openai_whisper"},
        env={},
    )
    app.state.voice_missing_reason = None
    app.state.logger = logging.getLogger("test.voice.routes")
    return app


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        (
            "missing_api_key",
            "Voice transcription failed: missing API key. Set OPENAI_API_KEY.",
        ),
        (
            "provider_unavailable",
            "Voice provider is unavailable.",
        ),
        (
            "local_provider_unavailable",
            "Local Whisper provider is unavailable.",
        ),
        (
            "local_runtime_dependency_missing",
            "Local voice runtime dependencies are unavailable.",
        ),
    ],
)
def test_voice_transcribe_returns_503_for_provider_setup_errors(
    reason: str,
    detail: str,
):
    class _FailingVoiceService:
        def transcribe(self, audio_bytes: bytes, **kwargs):
            raise VoicePermanentError(reason, detail)

    client = TestClient(_build_app(voice_service=_FailingVoiceService()))

    response = client.post("/api/voice/transcribe", content=b"voice-bytes")

    assert response.status_code == 503
    assert response.json() == {"detail": detail}
