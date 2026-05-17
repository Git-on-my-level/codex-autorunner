from __future__ import annotations

import importlib
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


def check_local_voice_provider_available(provider: Any) -> Optional[str]:
    """Check whether a local voice provider's Python package is importable.

    Returns ``None`` when the provider is available (or is not a local
    provider – e.g. ``openai_whisper``).  Returns a human-readable error
    string describing what is missing so the caller can log / raise
    appropriately.
    """
    spec = local_voice_provider_spec(provider)
    if spec is None:
        # Not a local provider (e.g. openai_whisper) – nothing to check here.
        return None

    _name, dep_groups, extra = spec
    for module_names, _display_name in dep_groups:
        candidates = (
            [module_names] if isinstance(module_names, str) else list(module_names)
        )
        for import_name in candidates:
            try:
                importlib.import_module(import_name)
                break
            except ImportError:
                continue
        else:
            return (
                f"Local voice provider '{_name}' is configured but its Python "
                f"package is not installed.  Install it with:\n"
                f"  pip install 'codex-autorunner[{extra}]'"
            )

    return None
