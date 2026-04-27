from __future__ import annotations

import shlex
from typing import Any, Sequence

DEFAULT_APP_SERVER_COMMAND: tuple[str, str] = ("codex", "app-server")


def parse_command(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        try:
            return [part for part in shlex.split(stripped) if part]
        except ValueError:
            return []
    if isinstance(raw, Sequence):
        parsed = [str(part).strip() for part in raw if str(part).strip()]
        return parsed
    return []


def resolve_app_server_command(
    configured_command: Any,
    *,
    fallback: Sequence[str] = DEFAULT_APP_SERVER_COMMAND,
) -> list[str]:
    configured = parse_command(configured_command)
    if configured:
        return configured
    return [str(part) for part in fallback]


__all__ = [
    "DEFAULT_APP_SERVER_COMMAND",
    "parse_command",
    "resolve_app_server_command",
]
