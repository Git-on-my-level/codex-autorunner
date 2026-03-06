from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple, Union

from ..core.utils import resolve_executable

OptionalDependency = Tuple[Union[str, Sequence[str]], str]

_LOCAL_PROVIDER_DEPS: dict[str, tuple[tuple[OptionalDependency, ...], str]] = {
    "local_whisper": (
        (("faster_whisper", "faster-whisper"),),
        "voice-local",
    ),
    "mlx_whisper": (
        (("mlx_whisper", "mlx-whisper"),),
        "voice-mlx",
    ),
}

_LOCAL_PROVIDER_RUNTIME_COMMANDS: dict[str, tuple[str, ...]] = {
    "local_whisper": ("ffmpeg",),
    "mlx_whisper": ("ffmpeg",),
}

_ALIASES = {
    "local": "local_whisper",
    "mlx": "mlx_whisper",
}


def normalize_voice_provider(provider: Any) -> str:
    if not isinstance(provider, str):
        return ""
    normalized = provider.strip().lower()
    if not normalized:
        return ""
    return _ALIASES.get(normalized, normalized)


def local_voice_provider_spec(
    provider: Any,
) -> Optional[tuple[str, tuple[OptionalDependency, ...], str]]:
    normalized = normalize_voice_provider(provider)
    spec = _LOCAL_PROVIDER_DEPS.get(normalized)
    if spec is None:
        return None
    deps, extra = spec
    return normalized, deps, extra


def missing_local_voice_runtime_commands(provider: Any) -> list[str]:
    normalized = normalize_voice_provider(provider)
    commands = _LOCAL_PROVIDER_RUNTIME_COMMANDS.get(normalized, ())
    return [command for command in commands if resolve_executable(command) is None]
