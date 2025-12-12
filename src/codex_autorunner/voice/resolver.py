from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

from .config import VoiceConfig
from .provider import SpeechProvider
from .providers import OpenAIWhisperProvider, build_speech_provider


def resolve_speech_provider(
    voice_config: VoiceConfig,
    logger: Optional[logging.Logger] = None,
    env: Optional[Mapping[str, str]] = None,
) -> SpeechProvider:
    """
    Resolve the configured speech provider. Raises when disabled or unknown.
    """
    if not voice_config.enabled:
        raise ValueError("Voice features are disabled in config")

    provider_name = voice_config.provider
    provider_configs = voice_config.providers or {}
    if not provider_name:
        raise ValueError("No voice provider configured")

    if provider_name == OpenAIWhisperProvider.name:
        return build_speech_provider(
            provider_configs.get(provider_name, {}),
            warn_on_remote_api=voice_config.warn_on_remote_api,
            env=env or os.environ,
            logger=logger,
        )

    raise ValueError(f"Unsupported voice provider '{provider_name}'")
