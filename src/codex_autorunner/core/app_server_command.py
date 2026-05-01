from __future__ import annotations

import dataclasses
import shlex
from typing import Any, Mapping, Sequence

DEFAULT_APP_SERVER_COMMAND: tuple[str, str] = ("codex", "app-server")
CODEX_APP_SERVER_COMMAND_ENV = "CAR_CODEX_APP_SERVER_COMMAND"
LEGACY_APP_SERVER_COMMAND_ENV = "CAR_APP_SERVER_COMMAND"
TELEGRAM_APP_SERVER_COMMAND_ENV = "CAR_TELEGRAM_APP_SERVER_COMMAND"
DEFAULT_CODEX_APP_SERVER_COMMAND_ENVS: tuple[str, str] = (
    CODEX_APP_SERVER_COMMAND_ENV,
    LEGACY_APP_SERVER_COMMAND_ENV,
)


@dataclasses.dataclass(frozen=True)
class ResolvedAppServerCommand:
    command: list[str]
    source: str
    ignored_env: tuple[str, ...] = ()


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


def resolve_app_server_command_with_source(
    configured_command: Any,
    *,
    env: Mapping[str, str] | None = None,
    env_names: Sequence[str] = DEFAULT_CODEX_APP_SERVER_COMMAND_ENVS,
    ignored_env_names: Sequence[str] = (TELEGRAM_APP_SERVER_COMMAND_ENV,),
    fallback: Sequence[str] = DEFAULT_APP_SERVER_COMMAND,
    fallback_source: str = "default",
) -> ResolvedAppServerCommand:
    source_env = env or {}
    ignored = tuple(
        name for name in ignored_env_names if str(source_env.get(name) or "").strip()
    )
    for name in env_names:
        command = parse_command(source_env.get(name))
        if command:
            return ResolvedAppServerCommand(
                command=command,
                source=f"env:{name}",
                ignored_env=ignored,
            )
    configured = parse_command(configured_command)
    if configured:
        return ResolvedAppServerCommand(
            command=configured,
            source="config",
            ignored_env=ignored,
        )
    return ResolvedAppServerCommand(
        command=[str(part) for part in fallback],
        source=fallback_source,
        ignored_env=ignored,
    )


__all__ = [
    "DEFAULT_APP_SERVER_COMMAND",
    "CODEX_APP_SERVER_COMMAND_ENV",
    "LEGACY_APP_SERVER_COMMAND_ENV",
    "TELEGRAM_APP_SERVER_COMMAND_ENV",
    "DEFAULT_CODEX_APP_SERVER_COMMAND_ENVS",
    "ResolvedAppServerCommand",
    "parse_command",
    "resolve_app_server_command",
    "resolve_app_server_command_with_source",
]
