from __future__ import annotations

import os
import shlex
from typing import Any, Iterable, Mapping, Optional, Sequence

GLOBAL_APP_SERVER_COMMAND_ENV = "CAR_APP_SERVER_COMMAND"
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


def iter_app_server_command_env_names(
    *extra_env_vars: Optional[str],
) -> tuple[str, ...]:
    names: list[str] = []
    for name in (GLOBAL_APP_SERVER_COMMAND_ENV, *extra_env_vars):
        normalized = str(name or "").strip()
        if normalized and normalized not in names:
            names.append(normalized)
    return tuple(names)


def resolve_app_server_command(
    configured_command: Any,
    *,
    env: Optional[Mapping[str, str]] = None,
    extra_env_vars: Iterable[Optional[str]] = (),
    fallback: Sequence[str] = DEFAULT_APP_SERVER_COMMAND,
) -> list[str]:
    source = env if env is not None else os.environ
    for env_name in iter_app_server_command_env_names(*extra_env_vars):
        env_value = source.get(env_name)
        command = parse_command(env_value)
        if command:
            return command
    configured = parse_command(configured_command)
    if configured:
        return configured
    return [str(part) for part in fallback]


__all__ = [
    "DEFAULT_APP_SERVER_COMMAND",
    "GLOBAL_APP_SERVER_COMMAND_ENV",
    "iter_app_server_command_env_names",
    "parse_command",
    "resolve_app_server_command",
]
